// checkpoint's mod: the native half of the plugin (Claude Code 2.1.287+ in
// the terminal, 2.1.286+ in the desktop app). Everything that must hold when
// mods are switched off is in the classic hooks (hooks/fast.sh); this adds
// what only a mod can do:
//
// - /check-pause [now]: an immediate command (it runs mid-turn). Writes the
//   pause flag (hooks/env.sh flag pause: the file fast.sh and Python read),
//   ends the running turn ($.turn.abort) and says at once what was paused and
//   what still runs. A running Bash call is left running (Claude Code keeps it
//   as a background shell), and the ack says so; with `now` it is stopped
//   first (Python's kill of its process tree), then the turn ends.
// - /check:pause [now] (the skill, so the name works without mods too) is
//   answered the same way when it runs; `all`, `<session>` and --stop-jobs go
//   on to the skill and its Python hook. Mod commands can't carry the plugin's
//   `check:` prefix (letters, digits, `_`, `-` only), and a bare `/pause` is
//   refused because it is the skill's own short name, hence `/check-pause`.
//   The plain keyword `pause [now]` typed while busy reaches prompt.submit at
//   once (with the running turn's id): it pauses there too.
// - A tool.call gate that holds every call while paused (but the read-only
//   tools and tracker writes the Python gate also allows), with a .catch that
//   denies: it fails closed.
// - No self-resume: a prompt that isn't the person's (isMeta, scheduled,
//   automation, notifications, other agents; a slash command a schedule or
//   /loop fires too) gets noresume/<session> before the classic hook sees
//   it, so it never resumes a paused session. If that mark can't be written
//   the prompt is held back (fails closed).
// - /check-doctor (immediate) and /check:doctor; /check:status answered from
//   Python without a model turn when the runtime is ready.
// - A status line: recording / paused / setting up / recording FAILED.
// - The journal (PLAN-v4 phase 2): turn.start / turn.complete, compactions
//   and calls its gate denied go to the session's spool (env.sh spool: the
//   same records fast.sh writes), and a timer has Python ingest the spool
//   every INGEST_EVERY_MS while it grows. /check-save (immediate) and
//   /check:save checkpoint now without stopping anything; /check-list lists
//   the checkpoints of this session's task.
// - Recovery (PLAN-v4 phase 3), immediate and usable while paused:
//   /check-resume [checkpoint] reconciles (Python), lifts the pause and sends
//   a prompt so the task goes on with the reconciliation in its context;
//   /check-cancel [interrupt|detach] ends the task (its jobs follow the
//   policy, stopped all at once; interrupt stops the running command before
//   the turn ends; the gate holds every tool call while it runs);
//   /check-stop <job> stops one job of checkpoint's runner; /check-diff
//   compares checkpoints; /check-list also opens a pane (Checkpoints) whose
//   rows diff against now or resume from that checkpoint.
// - Authorization references (0.7.0): a tool.check observer spools Claude
//   Code's permission verdict for each real call a settings rule, a hook or
//   an ask decided (decision, rule, deciding hook; the reason text goes only
//   to the spool, where Python keeps the gateway's right names from it), and
//   after an ask or a deny the gate spools what came of the call. It returns
//   the verdict beneath unchanged; a failure is skipped (the verdict stands).
//
// State lives on disk (CHECKPOINT_HOME, as fast.sh and state.py read it);
// module variables only hold what one process saw (the running turn, the
// calls in flight).
import type { EngineInterface, Register } from 'claude-code'
import {
  ackText, allowedTool, cancelDenyReason, clockOf, denyReason, GATE_FAILED, insideWorkDir, jobLive, MAX_DENIES,
  NORESUME_FAILED, parseCancelArgs, parseSelfArgs, parseSelfPause, RESUME_PROMPT, rowText, safeId, TRACKER_TOOLS,
  TYPED_ORIGINS, typedResumes,
} from './logic.ts'
import type { Call, CheckpointRow, Job } from './logic.ts'

type E = EngineInterface

const STATUS_EVERY_MS = 2000
const INGEST_EVERY_MS = 20000
const RESUME_EVERY_MS = 250
const GW_ACK_WAIT_MS = 1500 // how long the pause ack waits for the gateway's per-job outcomes
const PANE = 'check-checkpoints'

let turnId: string | undefined // the main loop's running turn (turn.start without agentId)
let denies = 0 // calls denied while paused in this turn
let pending = false // a pause is being written: the gate holds meanwhile
let memoryOnly = false // the pause flag couldn't be written: the gate holds anyway
let lastStatus: string | undefined
let home: string | undefined
let ingestedAt = 0 // the spool index's mtime when the timer last had it ingested
let ingesting = false
const inflight = new Map<string, Call>()
let resumeDue = false // /check-resume lifted the pause: the resume prompt goes in from the timer
let canceling = false // /check-cancel runs: the gate holds the session's tool calls until it is done
let paneTask = '' // the Checkpoints pane: whose checkpoints, the rows, and what a press showed
let paneRows: CheckpointRow[] = []
let paneNote: string[] = []
let gwNote = '' // the gateway's pause outcomes that came after the ack: the timer logs them
const verdicts = new Map<string, string>() // tool_use_id -> an ask or deny tool.check saw (until its call returns)
const VERDICTS_MAX = 200
// Claude Code's own text for a call it refused (2.1.301): a deny rule or
// hook, and the person's No at the permission prompt.
const REFUSAL_RE = /^(Permission to use .{1,400} has been denied|The user doesn't want to proceed with this tool use)/

async function stateHome($: E): Promise<string> {
  if (home !== undefined) return home
  const h = await $.env.get('CHECKPOINT_HOME')
  const x = await $.env.get('XDG_STATE_HOME')
  const u = await $.env.get('HOME')
  home = h ? h : x ? `${x}/checkpoint` : `${u ?? ''}/.local/state/checkpoint`
  return home
}

async function flagPath($: E): Promise<string> {
  return `${await stateHome($)}/flags/${safeId(await $.session.id())}`
}

async function isPaused($: E): Promise<boolean> {
  if (pending || memoryOnly) return true
  return $.fs.exists(await flagPath($))
}

/** The runtime env SessionStart recorded for this session (rt/<session>), or ''. */
async function recordedEnv($: E): Promise<string> {
  try {
    return (await $.fs.read(`${await stateHome($)}/rt/${safeId(await $.session.id())}`)).trim()
  } catch {
    return ''
  }
}

/** hooks/env.sh ARGS: the shell half that needs no Python. */
async function sh($: E, args: readonly string[], timeoutMs = 5000): Promise<{ ok: boolean; out: string; code: number }> {
  try {
    const envd = args[0] === 'flag' ? '' : await recordedEnv($)
    const r = await $.process.run(['/bin/sh', `${$.plugin.root}/hooks/env.sh`, ...args], {
      env: envd ? { CHECKPOINT_ENVD: envd } : {},
      timeoutMs,
    })
    return { ok: r.exitCode === 0, out: r.exitCode === 0 ? r.stdout : `${r.stderr}${r.stdout}`.trim(), code: r.exitCode }
  } catch (err) {
    return { ok: false, out: String(err).slice(0, 200), code: -1 }
  }
}

/** noresume/<session>: the prompt being submitted isn't the person's, so the
 * classic hook must not resume (env.sh flag noresume, else written here).
 * False when neither could write it. */
async function markNoresume($: E, sid: string): Promise<boolean> {
  if ((await sh($, ['flag', 'noresume', sid])).ok) return true
  try {
    await $.fs.write(`${await stateHome($)}/noresume/${safeId(sid)}`, `${new Date().toISOString()} mod\n`)
    return true
  } catch {
    return false
  }
}

/** One of the mod's events into the session's spool (env.sh spool). The
 * process is started here, synchronously, while the calling hook still runs
 * (a `$` call begun after a hook returned is refused); nobody waits for it,
 * and it never throws: the classic hooks record the same session, so a lost
 * mod event is a gap, not a failure to act on. */
function spool($: E, sid: string, event: string, data: Record<string, unknown>): void {
  try {
    $.process.run(['/bin/sh', `${$.plugin.root}/hooks/env.sh`, 'spool', sid, event], {
      stdin: JSON.stringify({ hook_event_name: event, session_id: sid, ...data }),
      timeoutMs: 5000,
    }).catch(() => undefined)
  } catch {
    // see above
  }
}

/** Every INGEST_EVERY_MS: when the spool index changed since the last time,
 * Python moves it into the journal (one at a time). */
async function ingestTick($: E): Promise<void> {
  if (ingesting) return
  try {
    const sid = await $.session.id()
    const st = await $.fs.stat(`${await stateHome($)}/spool/${safeId(sid)}/index`).catch(() => null)
    if (!st || st.mtimeMs <= ingestedAt) return
    if (!(await runtimeReady($))) return
    ingesting = true
    const r = await sh($, ['run', 'ingest', sid], 30000)
    if (r.ok) ingestedAt = st.mtimeMs
  } catch {
    // the next tick, or the next classic Python moment
  } finally {
    ingesting = false
  }
}

/** A recording failure this session hit (journal-errors/ or spool-errors/). */
async function recordingFailed($: E): Promise<boolean> {
  const h = await stateHome($)
  const sid = safeId(await $.session.id())
  return (await $.fs.exists(`${h}/journal-errors/${sid}`)) || (await $.fs.exists(`${h}/spool-errors/${sid}`))
}

/** The runtime for this session's plugin version: 'ready' (its env is built),
 * 'older' (an update's env is still building, and Python runs on the newest
 * previous env that finished: hooks/lib.sh ck_older, which `env.sh run`
 * uses), 'missing' (nothing built yet: the first run), or undefined (no
 * session start recorded which env). */
async function runtimeState($: E): Promise<'ready' | 'older' | 'missing' | undefined> {
  const envd = await recordedEnv($)
  if (!envd) return undefined
  if (await $.fs.exists(`${envd}/.ready`)) return 'ready'
  const cut = envd.lastIndexOf('/')
  const envs = cut > 0 ? envd.slice(0, cut) : ''
  if (!envs) return 'missing'
  try {
    for (const d of await $.fs.list(envs)) {
      // the build folders (`<hash>.building`, `.break`) have a dot; envs don't
      if (d.kind !== 'dir' || d.name.includes('.') || `${envs}/${d.name}` === envd) continue
      if ((await $.fs.exists(`${envs}/${d.name}/.ready`)) &&
          (await $.fs.exists(`${envs}/${d.name}/.pixi/envs/default/bin/python3`))) return 'older'
    }
  } catch {
    // no envs folder yet
  }
  return 'missing'
}

/** Can Python answer now (this version's env, or an older one while an
 * update's env builds)? undefined: no session start recorded which env. */
async function runtimeReady($: E): Promise<boolean | undefined> {
  const st = await runtimeState($)
  return st === undefined ? undefined : st !== 'missing'
}

async function refreshStatus($: E): Promise<void> {
  let text: string
  try {
    const rt = await runtimeState($)
    if (await isPaused($)) text = 'paused · type a message or /check:resume to resume'
    else if (rt === 'missing') text = 'setting up its runtime (first run); pausing works'
    else if (await recordingFailed($)) text = 'recording FAILED · /check-doctor'
    // An update's env builds in the background; the previous one records meanwhile.
    else if (rt === 'older') text = 'recording · updating its runtime in the background'
    else text = 'recording'
  } catch {
    text = 'state unknown (/check-doctor)'
  }
  if (text !== lastStatus) {
    lastStatus = text
    $.ui.status(text)
  }
}

async function endTurn($: E): Promise<string> {
  const t = turnId
  if (t === undefined) return 'No turn was running.'
  try {
    await $.turn.abort({ turnId: t })
    return 'Stopped the running turn.'
  } catch {
    return 'The turn had already ended.'
  }
}

async function runningJobs($: E, sid: string): Promise<Job[]> {
  try {
    const reg = JSON.parse(await $.fs.read(`${await stateHome($)}/jobs.json`)) as {
      jobs?: Record<string, { id?: string; name?: string; state?: string; session_id?: string; owner?: string; gone?: boolean }>
    }
    return Object.values(reg.jobs ?? {})
      .filter(j => j.session_id === sid && jobLive(j))
      .map(j => ({ id: String(j.id ?? ''), name: String(j.name ?? ''), supervisor: j.owner === 'gateway' ? 'gateway' : 'checkpoint' }))
  } catch {
    return []
  }
}

/** Where the gateway's daemon writes its discovery file (CONTRACT-v1;
 * CHECKPOINT_GATEWAY_CONTRACT overrides it, '' turns the integration off).
 * Python's gateway.discover makes the real checks; this only decides
 * whether to ask it. */
async function contractPath($: E): Promise<string> {
  const env = await $.env.get('CHECKPOINT_GATEWAY_CONTRACT')
  if (env !== undefined) return env
  return `${(await $.env.get('HOME')) ?? ''}/.config/gateway/run/contract.json`
}

/** The gateway's half of a pause (Python's gw-pause: POST /v1/pause for this
 * session's task, each job's on_pause policy): its line for the ack, '' when
 * there is no gateway. */
async function gatewayPause($: E, sid: string): Promise<string> {
  const path = await contractPath($)
  if (!path || !(await $.fs.exists(path).catch(() => false))) return ''
  if (!(await runtimeReady($))) return "Gateway: not told yet (checkpoint's runtime isn't ready); checkpoint tells it once it is."
  const r = await sh($, ['run', 'gw-pause', sid], 15000)
  try {
    return String((JSON.parse(r.out.trim().split('\n').at(-1) ?? '{}') as { text?: unknown }).text ?? '')
  } catch {
    return `Gateway: the pause couldn't be sent (exit ${r.code}); checkpoint tries again at its next moment (/check-doctor).`
  }
}

function mainBash(): Call[] {
  return [...inflight.values()].filter(c => c.tool === 'Bash' && c.agentId === undefined)
}

/** Pause this session now; returns the acknowledgement. */
async function pause($: E, now: boolean, via: string): Promise<string> {
  const t0 = Date.now()
  const sid = await $.session.id()
  const path = await flagPath($)
  const already = await $.fs.exists(path)
  let since = ''
  let wrote = true
  let writeError = ''
  pending = true
  try {
    if (already) {
      since = clockOf(await $.fs.read(path).catch(() => ''))
    } else {
      const r = await sh($, ['flag', 'pause', sid, via])
      if (!r.ok) {
        try {
          await $.fs.write(path, `${new Date().toISOString()} ${via}\n`)
        } catch (err) {
          wrote = false
          writeError = `${r.out}; ${String(err)}`.slice(0, 200)
        }
      }
    }
  } finally {
    pending = false
  }
  memoryOnly = !wrote
  // The gateway's half starts now, beside the rest (its jobs' pause policies).
  const gw = !already && wrote ? gatewayPause($, sid).catch(() => '') : null
  // What runs now, taken before anything ends: ending the turn resolves a
  // running Bash call (it moves to the background) and takes it out of inflight.
  const before = [...inflight.values()]
  const fg = mainBash()
  let stopped: Call[] = []
  let nowNote = ''
  if (now && fg.length) {
    // Stop the running command first (Python's `pause now`: the process tree
    // of each guarded foreground call), then end the turn; ending the turn
    // first would leave the command running as a background shell.
    if (!(await runtimeReady($))) {
      nowNote = "`now` couldn't stop it: checkpoint's runtime isn't ready."
    } else if (!(await sh($, ['flag', 'now', sid])).ok) {
      nowNote = "`now` couldn't ask for it to be stopped."
    } else {
      const r = await sh($, ['run', 'watch-fire', sid, fg[0]!.id], 15000)
      let mine = 0
      let others = 0
      let detail = ''
      try {
        const j = JSON.parse(r.out.trim().split('\n').at(-1) ?? '{}') as {
          killed?: { processes?: number; sigkill?: boolean; survivors?: number }[]; others?: number
        }
        mine = (j.killed ?? []).length
        others = Number(j.others ?? 0)
        const k = (j.killed ?? [])[0]
        if (k) {
          // Exactly what happened to it (Python's kill of its process tree).
          detail = `${k.processes ?? '?'} process(es) ended: ${k.sigkill ? 'SIGTERM, then SIGKILL for what was left' : 'SIGTERM'}${k.survivors ? `; ${k.survivors} survived SIGKILL` : ''}`
        }
      } catch {
        // no answer: nothing was stopped
      }
      stopped = [...(mine ? [{ ...fg[0]!, detail }] : []), ...fg.slice(1, 1 + others)]
      if (!stopped.length) nowNote = "`now` couldn't stop it (/check:status says why)."
    }
  }
  const turn = await endTurn($)
  const jobs = await runningJobs($, sid)
  const stoppedIds = new Set(stopped.map(c => c.id))
  const calls = [...stopped, ...before.filter(c => !stoppedIds.has(c.id))]
  void refreshStatus($)
  if (!already && wrote) void sh($, ['run', 'sync-pause', sid], 30000) // session record + checkpoint
  // The gateway's outcomes go into the ack when they come in time; later
  // ones are logged by the timer (the turn has already ended either way).
  let gateway = ''
  if (gw) {
    const late = Symbol('late')
    const got = await Promise.race([gw, $.clock.sleep(GW_ACK_WAIT_MS).then(() => late)])
    if (got === late) {
      gateway = 'Gateway: its jobs of this task are being paused (their pause policies); what happened follows in a moment.'
      void gw.then(t => {
        if (t) gwNote = t
      })
    } else {
      gateway = String(got)
    }
  }
  return ackText({ already, pausedSince: since, wrote, writeError, turn, calls, stopped, nowNote, jobs, gateway, tookMs: Date.now() - t0 })
}

/** Checkpoint now (Python's save: fsynced before it says saved). Never pauses. */
async function save($: E, why: string): Promise<string> {
  if (!(await runtimeReady($))) return "checkpoint: NOT saved: checkpoint's runtime isn't ready yet (first-time setup); what happens meanwhile waits in the spool."
  const sid = await $.session.id()
  const r = await sh($, ['run', 'save', sid, ...(why.trim() ? [why.trim()] : [])], 60000)
  const out = r.out.trim()
  return out ? `checkpoint: ${out}` : `checkpoint: NOT saved (exit ${r.code}); /check-doctor says why.`
}

async function listCheckpoints($: E): Promise<string> {
  if (!(await runtimeReady($))) return "checkpoint: checkpoint's runtime isn't ready yet (first-time setup)."
  const r = await sh($, ['run', 'list', await $.session.id()], 30000)
  return r.out.trim() || `checkpoint: listing failed (exit ${r.code}); /check-doctor says why.`
}

/** The immediate /check-resume [checkpoint]: Python reconciles and lifts the
 * pause (the resume stays pending), then a prompt goes in so the task goes on
 * at once, the classic hook adding the reconciliation to it. */
async function resumeNow($: E, ref: string): Promise<string> {
  const held = memoryOnly
  if (!(await runtimeReady($))) {
    if (held) {
      memoryOnly = false
      void refreshStatus($)
      return 'checkpoint: resumed (the pause was held in memory only); without the runtime there is no reconciliation: check the files and anything that was running before going on.'
    }
    return "checkpoint: /check-resume reconciles first, and checkpoint's runtime isn't ready yet (first-time setup). Type a message to resume without it (no resume notes), or try again in a minute."
  }
  const sid = await $.session.id()
  const r = await sh($, ['run', 'resume-now', sid, ...(ref.trim() ? [ref.trim()] : [])], 60000)
  const out = r.out.trim()
  if (!r.ok) return `checkpoint: ${out || `resume failed (exit ${r.code})`}`
  memoryOnly = false
  void refreshStatus($)
  const goes = held || !out.startsWith("This session wasn't paused")
  // A prompt submitted inside a command's hook never entered (measured, 2.1.301:
  // the session is busy with the command until the hook returns, and a $ call
  // left for later is refused): the session.start timer sends it.
  if (goes) resumeDue = true
  return `checkpoint: ${out}${goes ? ' The task goes on now (checkpoint sends the resume prompt).' : ''}`
}

/** From the session.start timer: the resume prompt /check-resume asked for,
 * and the gateway's pause outcomes that came after the ack. */
function sendResume($: E): void {
  if (gwNote) {
    const note = gwNote
    gwNote = ''
    try {
      $.ui.log(`checkpoint: ${note}`.replace(/\n/g, ' '))
    } catch {
      // the ack said they would follow; /check:status shows the jobs
    }
  }
  if (!resumeDue) return
  resumeDue = false
  try {
    $.prompt.submit({ text: RESUME_PROMPT }).catch(() => {
      $.ui.log('checkpoint: sending the resume prompt failed; send any message to continue (the resume notes come with it).')
    })
  } catch {
    $.ui.log('checkpoint: sending the resume prompt failed; send any message to continue (the resume notes come with it).')
  }
}

/** The immediate /check-cancel: the task ends (Python: the running command,
 * then its jobs by policy, all at once, then the record), and so does the
 * running turn. The gate holds every tool call from the start until it is
 * done: while Python stops things the model can't act (a job_start then
 * would escape the cancel). With interrupt the running command is stopped
 * before the turn ends (ending it first would leave it running in the
 * background); with detach the turn ends first and the command stays. */
async function cancelTask($: E, args: string): Promise<string> {
  if (!(await runtimeReady($))) return "checkpoint: NOT canceled: checkpoint's runtime isn't ready yet (first-time setup); /check:cancel after it is."
  const { policy } = parseCancelArgs(args)
  const sid = await $.session.id()
  let turn = ''
  canceling = true
  try {
    if (policy === 'detach') turn = await endTurn($)
    const r = await sh($, ['run', 'cancel', sid, ...args.trim().split(/\s+/).filter(Boolean)], 60000)
    if (policy !== 'detach') turn = await endTurn($)
    memoryOnly = false
    void refreshStatus($)
    const out = r.out.trim()
    return out ? `checkpoint: ${out} ${turn}` : `checkpoint: cancel failed (exit ${r.code}); /check-doctor says why.`
  } finally {
    canceling = false
  }
}

async function runPy($: E, what: string, args: readonly string[], timeoutMs = 30000): Promise<string> {
  if (!(await runtimeReady($))) return "checkpoint: checkpoint's runtime isn't ready yet (first-time setup)."
  const r = await sh($, ['run', what, await $.session.id(), ...args], timeoutMs)
  return r.out.trim() || `checkpoint: ${what} failed (exit ${r.code}); /check-doctor says why.`
}

/** The Checkpoints pane's rows (Python's list-json). */
async function loadPane($: E): Promise<void> {
  const r = await sh($, ['run', 'list-json', await $.session.id()], 30000)
  try {
    const j = JSON.parse(r.out.trim()) as { task?: string; checkpoints?: CheckpointRow[] }
    paneTask = String(j.task ?? '')
    paneRows = (j.checkpoints ?? []).slice(-12)
  } catch {
    paneTask = ''
    paneRows = []
  }
}

async function paneDiff($: E, id: string): Promise<void> {
  const out = await runPy($, 'diff-session', [id, 'now'])
  paneNote = out.split('\n').slice(0, 40)
  $.ui.invalidate('ui.render')
}

async function paneResume($: E, id: string): Promise<void> {
  paneNote = [await resumeNow($, id)]
  $.ui.invalidate('ui.render')
}

async function doctor($: E): Promise<string> {
  const v = await $.session.version().catch(() => undefined)
  const surfaces = await $.session.surfaces().catch(() => [] as readonly string[])
  const sid = await $.session.id().catch(() => '')
  const r = await sh($, ['doctor', sid], 20000)
  const lines = [
    'checkpoint doctor',
    `- Claude Code: ${v?.version ?? '?'}; mods: on (this report is the mod's; mods need 2.1.287 in the terminal, 2.1.286 in the desktop app)`,
    `- surfaces: ${surfaces.length ? surfaces.join(', ') : 'none'}`,
    '- immediate commands: /check-pause [now|interrupt], /check-resume [checkpoint], /check-cancel [interrupt|detach], /check-stop <job>, /check-save, /check-list (pane), /check-diff, /check-doctor; /check:pause, /check:save, /check:status and /check:doctor are answered at once when they run',
    ...(r.out.trim() ? r.out.trim().split('\n').map(l => `- ${l}`) : ['- runtime report: hooks/env.sh doctor printed nothing']),
  ]
  if (!r.ok) lines.push(`- runtime report failed (exit ${r.code})`)
  return lines.join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    const failed = (name: string) => (err: unknown) =>
      $.ui.log(`checkpoint: /${name} is unavailable: ${String(err).slice(0, 200)}`, { to: 'debug' })
    await $.command.register({
      name: 'check-pause',
      description: 'Pause this task now, even mid-turn: stops the running turn and holds tool calls (checkpoint)',
      argumentHint: '[now]',
      immediate: true,
    }).catch(failed('check-pause'))
    await $.command.register({
      name: 'check-doctor',
      description: "checkpoint's diagnostics, at once: versions, runtime, gate, state",
      immediate: true,
    }).catch(failed('check-doctor'))
    await $.command.register({
      name: 'check-save',
      description: 'Checkpoint now, even mid-turn: records the task state (synced to disk) without stopping anything (checkpoint)',
      argumentHint: '[why]',
      immediate: true,
    }).catch(failed('check-save'))
    await $.command.register({
      name: 'check-list',
      description: "List the checkpoints of this session's task, in a pane to diff or resume from one (checkpoint)",
      immediate: true,
    }).catch(failed('check-list'))
    await $.command.register({
      name: 'check-resume',
      description: 'Resume the paused task now, after reconciling it (or from a checkpoint); works while paused (checkpoint)',
      argumentHint: '[checkpoint]',
      immediate: true,
    }).catch(failed('check-resume'))
    await $.command.register({
      name: 'check-cancel',
      description: 'Cancel this task now: its jobs are stopped (interrupt, default) or left running (detach); history kept (checkpoint)',
      argumentHint: '[interrupt|detach]',
      immediate: true,
    }).catch(failed('check-cancel'))
    await $.command.register({
      name: 'check-stop',
      description: "Stop one detached job of checkpoint's runner now; without a name, list them (checkpoint)",
      argumentHint: '[job]',
      immediate: true,
    }).catch(failed('check-stop'))
    await $.command.register({
      name: 'check-diff',
      description: 'What changed between two checkpoints, or a checkpoint and now (checkpoint)',
      argumentHint: '[checkpoint [checkpoint|now]]',
      immediate: true,
    }).catch(failed('check-diff'))
    void refreshStatus($)
    $.clock.every(STATUS_EVERY_MS, () => void refreshStatus($))
    $.clock.every(INGEST_EVERY_MS, () => void ingestTick($))
    $.clock.every(RESUME_EVERY_MS, () => sendResume($))
    return started
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      turnId = e.turnId
      denies = 0
    }
    const sid = await $.session.id().catch(() => '')
    if (sid) spool($, sid, 'mod.turn.start', { turnId: e.turnId, agent_id: e.agentId, chars: e.text.length })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && e.turnId === turnId) turnId = undefined
    const sid = await $.session.id().catch(() => '')
    if (sid) spool($, sid, 'mod.turn.complete', { turnId: e.turnId, agent_id: e.agentId, reason: e.reason, durationMs: e.durationMs })
    return next(e)
  })

  // Compactions: the classic PreCompact/PostCompact hooks record them too;
  // this adds the trigger for a subagent's own and whether it was skipped.
  on('session.compact', async ($, e, next) => {
    if (e.trigger === 'precompute') return next(e)
    const sid = await $.session.id().catch(() => '')
    if (sid) spool($, sid, 'mod.compact', { phase: 'start', trigger: e.trigger, agent_id: e.agentId })
    const r = await next(e)
    if (sid) spool($, sid, 'mod.compact', { phase: 'done', trigger: e.trigger, agent_id: e.agentId, result: r.skip !== undefined ? 'skipped' : 'compacted' })
    return r
  })

  // Authorization references: Claude Code's verdict for a real call (one
  // with a tool_use_id; a $.tool.check query has none) that a rule, a hook or
  // an ask decided. An observer: the verdict beneath goes back as it is.
  on('tool.check', async ($, e, next) => {
    const v = await next(e)
    const id = e.tool_use_id
    if (id && (v.decision !== 'allow' || v.rule || v.hook)) {
      if (v.decision !== 'allow') {
        if (verdicts.size >= VERDICTS_MAX) verdicts.delete(verdicts.keys().next().value as string)
        verdicts.set(id, v.decision)
      }
      const sid = await $.session.id().catch(() => '')
      if (sid) {
        spool($, sid, 'mod.tool.check', {
          tool_use_id: id, tool: e.tool, agent_id: e.agentId, decision: v.decision, rule: v.rule, hook: v.hook,
          reason: typeof v.reason === 'string' ? v.reason.slice(0, 600) : undefined,
        })
      }
    }
    return v
  })

  // The gate. Fails closed: if it can't tell, the .catch denies.
  on('tool.call', async ($, e, next) => {
    const name = String(e.tool)
    const cancel = canceling
    if (cancel || (await isPaused($))) {
      let allowed = allowedTool(name)
      if (!allowed && TRACKER_TOOLS.includes(name)) {
        const args = e as unknown as { file_path?: unknown; notebook_path?: unknown }
        const target = String(args.file_path ?? args.notebook_path ?? '')
        const cwd = await $.session.cwd()
        const work = (await $.env.get('CHECKPOINT_WORK_DIR')) || '.work'
        allowed = insideWorkDir(target, [await $.session.root(), cwd], work, cwd)
      }
      if (!allowed) {
        if (e.agentId === undefined) {
          denies += 1
          if (denies > MAX_DENIES) void endTurn($)
        }
        // The journal: this call never ran (its intent must not read as in flight).
        spool($, await $.session.id(), 'mod.tool.denied', { tool_use_id: e.tool_use_id, tool: name, agent_id: e.agentId, why: cancel ? 'canceling' : 'paused' })
        return { deny: cancel ? cancelDenyReason(e.agentId !== undefined) : denyReason(e.agentId !== undefined) }
      }
    }
    const id = e.tool_use_id ?? `${name}-${Date.now()}-${inflight.size}`
    const command = name === 'Bash' ? String((e as unknown as { command?: unknown }).command ?? '') : undefined
    inflight.set(id, { id, tool: name, command, agentId: e.agentId, started: Date.now() })
    try {
      const r = await next(e)
      const asked = e.tool_use_id ? verdicts.get(e.tool_use_id) : undefined
      if (asked !== undefined && e.tool_use_id) {
        // What came of an ask (the person's answer, or auto mode's) or a deny:
        // refused, an error, or a result (Python adds the call's own outcome).
        verdicts.delete(e.tool_use_id)
        const answered = typeof (r as { deny?: unknown }).deny === 'string' ? 'deny'
          : (r as { isError?: unknown }).isError === true ? 'error' : 'result'
        // Claude Code 2.1.301 answers a refused call with an error result in
        // its own words (a deny rule; the person's No): that it never ran.
        const text = String((r as { text?: unknown }).text ?? '')
        const rejected = answered === 'error' && REFUSAL_RE.test(text)
        const sid = await $.session.id().catch(() => '')
        if (sid) spool($, sid, 'mod.tool.answer', { tool_use_id: e.tool_use_id, tool: name, agent_id: e.agentId, answered, rejected })
      }
      return r
    } finally {
      inflight.delete(id)
    }
  }).catch(($, e, next) => (next.called ? next(e) : { deny: GATE_FAILED }))

  // No self-resume, and `pause` typed while a turn runs. A plain prompt
  // typed mid-turn reaches prompt.submit at once (turnId set; a slash
  // command waits in the queue instead): pause here, then let it go on, so
  // the classic hook answers it ("already paused") once the turn is over. A
  // typed prompt is never dropped: a dropped prompt comes back into the
  // prompt box.
  on('prompt.submit', async ($, e, next) => {
    // Who sent it comes first, slash commands included: a scheduled task or
    // /loop fires its stored prompt, often a slash command, and that must
    // not resume a paused session either.
    const typed = e.isMeta !== true && TYPED_ORIGINS.has(e.origin.kind)
    const kw = typed && !e.command && e.turnId !== undefined ? parseSelfPause(e.text) : null
    if (kw) {
      $.ui.log((await pause($, kw.now, 'typed pause')).replace(/\n/g, ' ')) // a log line is one line
      return next(e)
    }
    const paused = await isPaused($)
    if (paused && !typed) {
      const sid = await $.session.id()
      // Unmarked, the classic hook would take it as the person's and resume:
      // if the mark can't be written, hold the prompt back (fails closed).
      if (!(await markNoresume($, sid))) return { drop: NORESUME_FAILED }
      try {
        return await next(e)
      } finally {
        await sh($, ['flag', 'clear-noresume', sid])
      }
    }
    // The person's message resumes (the classic hook clears the flag): a
    // plain message, /check:resume or another slash command, but not a pause
    // keyword (`pause all`) or /check:status. A pause held only in memory
    // (the flag couldn't be written) is lifted here, as the classic hook
    // would lift the flag.
    if (paused && typedResumes(e.text, e.command) && memoryOnly) {
      memoryOnly = false
      $.ui.log('checkpoint: resumed this session (its pause was held in memory only: the flag could not be written).')
    }
    const entered = await next(e)
    if (paused) void refreshStatus($)
    return entered
  }).catch(($, e, next) =>
    next.called || (e.isMeta !== true && TYPED_ORIGINS.has(e.origin.kind))
      ? next(e)
      : { drop: "checkpoint: held back a message that isn't yours, because checking the pause failed" })

  on('command.run', { command: 'check-pause' }, async ($, e) => {
    const a = parseSelfArgs(e.args)
    if (a === null) {
      return { text: 'checkpoint: /check-pause pauses this session (`now` also stops its running command). For other sessions: /check:pause all, or /check:pause <session>.' }
    }
    return { text: await pause($, a.now, '/check-pause') }
  }).catch(() => ({ text: "checkpoint: /check-pause failed; type `pause` or /check:pause instead (they don't need the mod)." }))

  on('command.run', { command: 'check:pause' }, async ($, e, next) => {
    const a = parseSelfArgs(e.args)
    if (a === null) return next(e) // all / <session> / --stop-jobs: the skill and its hook
    return { text: await pause($, a.now, '/check:pause') }
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'check-save' }, async ($, e) => ({ text: await save($, e.args) }))
    .catch(() => ({ text: "checkpoint: /check-save failed; /check:save asks the runtime instead (it doesn't need the mod)." }))

  on('command.run', { command: 'check:save' }, async ($, e, next) => {
    if (!(await runtimeReady($))) return next(e) // the skill and its hook say what happened
    return { text: await save($, e.args) }
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'check-list' }, async $ => {
    const text = await listCheckpoints($)
    if (await runtimeReady($)) {
      await loadPane($)
      paneNote = []
      void $.ui.open({ id: PANE, title: 'Checkpoints', focus: true, closeOnEscape: true }).catch(() => undefined)
    }
    return { text }
  }).catch(() => ({ text: 'checkpoint: /check-list failed; /check:list asks the runtime instead.' }))

  // The Checkpoints pane: each row diffs against now, or resumes from there.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        <Text bold>{paneTask ? `Checkpoints of task ${paneTask}` : 'No task journal yet'}</Text>
        {paneRows.length === 0 && <Text dimColor>none yet: /check-save makes one (each pause saves one too)</Text>}
        {paneRows.map(r => (
          <Box key={`row-${r.id}`} flexDirection="column">
            <Text>{rowText(r)}</Text>
            <Box flexDirection="row">
              <Button key={`diff-${r.id}`} dimColor onPress={() => void paneDiff($, r.id)}>diff with now</Button>
              <Button key={`resume-${r.id}`} dimColor onPress={() => void paneResume($, r.id)}>resume from here</Button>
            </Box>
          </Box>
        ))}
        {paneNote.map((l, i) => <Text key={`note-${i}`}>{l || ' '}</Text>)}
        <Button key="close" role="dismiss" onPress={() => void $.ui.close({ id: PANE })}>close</Button>
      </Box>
    )
  })

  on('command.run', { command: 'check-resume' }, async ($, e) => ({ text: await resumeNow($, e.args) }))
    .catch(() => ({ text: "checkpoint: /check-resume failed; type a message or /check:resume (they don't need the mod)." }))

  on('command.run', { command: 'check-cancel' }, async ($, e) => ({ text: await cancelTask($, e.args) }))
    .catch(() => ({ text: 'checkpoint: /check-cancel failed; /check:cancel does the same without the mod.' }))

  on('command.run', { command: 'check-stop' }, async ($, e) => ({
    text: `checkpoint: ${await runPy($, 'stop-job', e.args.trim().split(/\s+/).filter(Boolean), 30000)}`,
  })).catch(() => ({ text: 'checkpoint: /check-stop failed; /check:stop does the same without the mod.' }))

  on('command.run', { command: 'check-diff' }, async ($, e) => ({
    text: await runPy($, 'diff-session', e.args.trim().split(/\s+/).filter(Boolean)),
  })).catch(() => ({ text: 'checkpoint: /check-diff failed; /check:diff does the same without the mod.' }))

  on('command.run', { command: 'check-doctor' }, async $ => ({ text: await doctor($) }))
    .catch(() => ({ text: 'checkpoint: /check-doctor failed; /check:doctor asks the runtime instead.' }))

  on('command.run', { command: 'check:doctor' }, async $ => ({ text: await doctor($) }))
    .catch(($, e, next) => next(e))

  on('command.run', { command: 'check:status' }, async ($, e, next) => {
    const r = await sh($, ['run', 'status', '--session', await $.session.id()], 30000)
    return r.ok && r.out.trim() ? { text: r.out.trim() } : next(e) // no runtime yet: the skill says so
  }).catch(($, e, next) => next(e))
}
