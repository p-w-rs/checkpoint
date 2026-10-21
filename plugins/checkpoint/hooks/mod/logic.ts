// Pure helpers for checkpoint's mod (register.tsx): ids, what stays allowed
// while paused, the pause forms it answers, and the texts it shows. No `$`
// here, so the mod's tests (tests/mod/) can check them directly.
//
// Kept in step with the Python side (tests/run_tests.py compares them):
// ALLOW_WHILE_PAUSED and MAX_DENIES with src/checkpoint/state.py, safeId with
// state.safe_id and hooks/lib.sh ck_safe, the paths with state.py's layout.

/** Tools a paused session may still call (state.ALLOW_WHILE_PAUSED). */
export const ALLOW_WHILE_PAUSED: readonly string[] = [
  'Read', 'Glob', 'Grep', 'TodoWrite', 'TaskList', 'TaskGet', 'TaskCreate', 'TaskUpdate',
  'mcp__ccd_turn__*',
  'mcp__plugin_check_jobs__job_status',
  'mcp__plugin_check_jobs__job_tail',
  'mcp__plugin_check_jobs__job_stop',
  'mcp__plugin_check_jobs__checkpoint_note',
  'mcp__plugin_check_jobs__task_policy',
  'mcp__plugin_gate_gw__terminal_read',
  'mcp__plugin_gate_gw__terminal_list',
  'mcp__plugin_gate_gw__job_status',
  'mcp__plugin_gate_gw__job_tail',
  'mcp__plugin_gate_gw__job_list',
]

/** Writes allowed while paused when they land in the project's work dir (trackers). */
export const TRACKER_TOOLS: readonly string[] = ['Write', 'Edit', 'MultiEdit', 'NotebookEdit']

/** Denied calls in one turn while paused before the turn is ended (state.MAX_DENIES). */
export const MAX_DENIES = 3

/** Prompt origins that are the person typing (or sending from a phone or the
 * desktop app): only these resume a paused session or count as a typed pause. */
export const TYPED_ORIGINS: ReadonlySet<string> = new Set(['composer', 'bridge', 'sdk'])

/** A session id as a file name: state.safe_id, ck_safe in hooks/lib.sh. */
export function safeId(s: string): string {
  return (s || '').replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 128) || '_'
}

/** fnmatch for the allow list: a pattern is a name, or a prefix ending in `*`. */
export function allowedTool(name: string): boolean {
  return ALLOW_WHILE_PAUSED.some(p => (p.endsWith('*') ? name.startsWith(p.slice(0, -1)) : name === p))
}

/** Is `path` (absolute, or relative to `cwd`) inside `<dir>/<work>/` for one
 * of `dirs`? Lexical: a path with `..` never is. The classic gate checks the
 * same call again with symlinks resolved (hooks.allowed_while_paused). */
export function insideWorkDir(path: string, dirs: readonly string[], work: string, cwd: string): boolean {
  if (!path || !work || work.includes('..')) return false
  const abs = path.startsWith('/') ? path : `${cwd}/${path}`
  if (/(^|\/)\.\.(\/|$)/.test(abs)) return false
  const norm = abs.replace(/\/+/g, '/').replace(/\/\.(?=\/|$)/g, '')
  return dirs.some(d => {
    if (!d || !d.startsWith('/')) return false
    const w = `${d.replace(/\/+$/, '')}/${work.replace(/^\/+|\/+$/g, '')}/`
    return norm.startsWith(w)
  })
}

/** The pause forms the mod answers itself: this session only, with or without
 * `now`. `all`, another session's id and `--stop-jobs` are left to the
 * /check:pause skill and its Python hook (they reach other sessions and jobs). */
export type SelfPause = { now: boolean }

/** `/check:pause [now|interrupt]`, or the plain keyword `pause [now|interrupt]`
 * (as transcript.parse_command): `interrupt` is `now`. */
export function parseSelfPause(text: string): SelfPause | null {
  const t = (text || '').trim()
  const cmd = /^\/check:pause(?:\s+(now|interrupt))?\s*$/i.exec(t)
  if (cmd) return { now: cmd[1] !== undefined }
  const kw = /^pause(?:\s+(now|interrupt))?[.!]?$/i.exec(t)
  if (kw) return { now: kw[1] !== undefined }
  return null
}

/** Does a prompt the person typed resume a paused session? As
 * hooks.handle_prompt decides with the flag on disk: `/check:resume` does,
 * the other /check:* commands (pause, status, doctor, help) and the `pause`
 * keyword (transcript.KEYWORD_RE) don't, and any other message or slash
 * command does. The mod uses it for a pause it could only hold in memory. */
export function typedResumes(text: string, command?: { name: string }): boolean {
  if (command) {
    const n = command.name
    if (n === 'check:resume') return true
    return !(n.startsWith('check:') || n.startsWith('check-'))
  }
  const t = (text || '').trim()
  const cmd = /^\/check:(pause|resume|status|help|doctor|save|list|diff|cancel|stop|complete|export)(?:\s|$)/.exec(t)
  if (cmd) return cmd[1] === 'resume'
  return !/^pause(?:\s+(now|interrupt|all|all\s+now|now\s+all|all\s+interrupt|interrupt\s+all))?[.!]?$/i.test(t)
}

/** A command's arguments (`/check-pause now`, `/check:pause`): '' or `now`
 * (`interrupt` is the same: the in-flight command is stopped at once). */
export function parseSelfArgs(args: string): SelfPause | null {
  const a = (args || '').trim().toLowerCase()
  if (a === '') return { now: false }
  if (a === 'now' || a === 'interrupt') return { now: true }
  return null
}

/** `/check-cancel [interrupt|detach] [note]`: the child-work policy for the
 * task's running work ('' = each job's own, interrupt by default). */
export function parseCancelArgs(args: string): { policy: '' | 'interrupt' | 'detach'; note: string } {
  const words = (args || '').trim().split(/\s+/).filter(Boolean)
  const policy = (words.find(w => w === 'interrupt' || w === 'detach') ?? '') as '' | 'interrupt' | 'detach'
  return { policy, note: words.filter(w => w !== policy).join(' ') }
}

/** What the mod submits after an immediate /check-resume, so the task goes on
 * at once: the classic hook adds the reconciliation and the recovery summary
 * to it (the resume is pending), and the model reads it as the person's ask. */
export const RESUME_PROMPT =
  'Resume the paused task (I typed /check-resume). Do what checkpoint\'s reconciliation lists under "Check first" before anything else, then continue.'

/** A one-line row for a checkpoint in the pane (and its fallback text). */
export type CheckpointRow = {
  id: string; time: string; seq_from: number; seq_to: number; line: string; staleness: string
  stale_why?: string[]; newest?: boolean; reason?: string
}

export function rowText(r: CheckpointRow): string {
  const st = r.staleness === 'current' ? 'current' : `stale: ${(r.stale_why ?? []).join(', ')}`
  return `${r.id}  ${r.time}  seq ${r.seq_from}-${r.seq_to}  ${r.line}  [${st}]${r.newest ? ' (newest)' : ''}`
}

/** What a denied call's model reads (as hooks.gate words it). */
export function denyReason(isSubagent: boolean): string {
  return isSubagent
    ? "Paused by the user (checkpoint). Don't call more tools: stop here and return a short report of what you did and what is left."
    : "Paused by the user (checkpoint). Don't start new work. Bring the tracker up to date if there is one (Write/Edit to it are allowed), then end your turn with one line saying you're paused."
}

/** What a call denied while /check-cancel runs reads: the task is ending,
 * its jobs are being stopped, nothing new may start meanwhile (a job_start
 * then would escape the cancel). */
export function cancelDenyReason(isSubagent: boolean): string {
  return isSubagent
    ? "The user is canceling this task (checkpoint). Don't call more tools: stop here and return a one-line report."
    : "The user is canceling this task (checkpoint): its work is being stopped. Don't call more tools or start anything; end your turn with one line."
}

export const GATE_FAILED =
  "checkpoint: checking whether this session is paused failed, so this call is held. Try again; /check-doctor says what is wrong."

/** A prompt that isn't the person's, held back from a paused session because
 * marking it as not theirs (noresume/<session>) failed: let through
 * unmarked, the classic hook would resume the session. */
export const NORESUME_FAILED =
  "checkpoint: held back a message that isn't yours: this session is paused, and marking the message so it doesn't resume the session failed (/check-doctor)"

/** One running tool call the mod saw start (tool.call) and not yet end. */
export type Call = { id: string; tool: string; command?: string; agentId?: string; started: number; detail?: string }

/** A job from jobs.json as the ack names it (`supervisor`: who runs it). */
export type Job = { id: string; name: string; supervisor?: 'gateway' | 'checkpoint' }

/** A gateway job's states that mean it may still run (gateway.LIVE_STATES;
 * unknown, gone or not, counts too: the contract's unknown is never finished): jobs.is_live. */
export const GATEWAY_LIVE: readonly string[] = ['starting', 'running', 'waiting_input', 'waiting_auth', 'suspended', 'disconnected']

/** Is a jobs.json record live (running or possibly running), as jobs.is_live decides? */
export function jobLive(j: { owner?: string; state?: string; gone?: boolean }): boolean {
  if (j.owner === 'gateway') return GATEWAY_LIVE.includes(String(j.state)) || j.state === 'unknown'
  return j.state === 'running' || j.state === 'stop_failed'
}

export type PauseReport = {
  already: boolean
  pausedSince?: string
  wrote: boolean
  writeError?: string
  turn: string
  calls: readonly Call[]
  stopped: readonly Call[]
  nowNote?: string
  jobs: readonly Job[]
  gateway?: string
  tookMs: number
}

function quote(cmd: string | undefined): string {
  const one = (cmd || '').replace(/\s+/g, ' ').trim()
  return '`' + (one.length > 80 ? one.slice(0, 77) + '...' : one) + '`'
}

/** The acknowledgement: what was paused, and what still runs. */
export function ackText(r: PauseReport): string {
  const lines: string[] = []
  if (r.already) {
    lines.push(`checkpoint: this session is already paused${r.pausedSince ? ` (since ${r.pausedSince})` : ''}. ${r.turn}`)
  } else {
    lines.push(`checkpoint: paused this session (at once, in ${(r.tookMs / 1000).toFixed(2)} s). ${r.turn}`)
  }
  if (!r.wrote) {
    lines.push(`Couldn't record the pause on disk (${r.writeError || 'unknown error'}): this session's tool calls are held while it runs, but a restart would lose the pause.`)
  }
  const stopped = new Set(r.stopped)
  for (const c of r.stopped) lines.push(`Stopped Bash ${quote(c.command)} (pause now${c.detail ? ': ' + c.detail : ''}).`)
  const bash = r.calls.filter(c => c.tool === 'Bash' && !stopped.has(c))
  for (const c of bash) {
    const who = c.agentId ? 'a subagent\'s ' : ''
    lines.push(`Still running: ${who}Bash ${quote(c.command)}, left running (Claude Code keeps it as a background shell; stop it from its shell list, or pause with \`now\` next time).${r.nowNote ? ' ' + r.nowNote : ''}`)
  }
  const others = r.calls.filter(c => c.tool !== 'Bash')
  if (others.length) {
    lines.push(`Also in flight: ${others.map(c => c.tool).join(', ')} (it finishes; nothing new starts).`)
  }
  const own = r.jobs.filter(j => j.supervisor !== 'gateway')
  if (own.length) {
    lines.push(`Detached jobs keep running (checkpoint's runner): ${own.map(j => `${j.name} (${j.id})`).join(', ')}.`)
  }
  if (r.gateway) {
    lines.push(r.gateway)
  } else {
    const gw = r.jobs.filter(j => j.supervisor === 'gateway')
    if (gw.length) lines.push(`Gateway jobs, not told of the pause (their pause policies didn't apply): ${gw.map(j => `${j.name} (${j.id})`).join(', ')}.`)
  }
  lines.push('Tool calls stay held until you resume: type a message, or /check:resume.')
  return lines.join('\n')
}

/** HH:MM:SS of an ISO-ish time (`2026-10-19T19:12:00+0200` or `...Z`). */
export function clockOf(iso: string): string {
  const m = /T(\d\d:\d\d:\d\d)/.exec(iso || '')
  return m ? m[1]! : ''
}
