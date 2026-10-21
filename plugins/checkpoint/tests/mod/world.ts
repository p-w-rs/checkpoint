// The world beneath checkpoint's mod for `claude plugin test` (the mod's
// tests import it): a file system in memory, hooks/env.sh simulated (flag
// writes, doctor, the Python commands), the session, the turn and the tool
// calls. `answers` lets a test give a `run <what>` its own reply.
import { expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MockClock } from 'claude-code/testing'

export const HOME = '/state'
export const SID = 'sess-1'
export const FLAG = `${HOME}/flags/${SID}`
export const ROOT = '/proj'

export type World = {
  files: Map<string, string>
  runs: string[][]
  aborts: string[]
  statuses: (string | undefined)[]
  logs: string[]
  registered: { name: string; immediate?: true }[]
  calls: string[]
  release: Map<string, () => void>
  noresumeSeen: boolean[]
  clock: MockClock
  submitted: string[]
  events: string[]
  opened: string[]
  closed: string[]
}

/** A finished process as $.process.run answers it. */
export function ran(exitCode: number, stdout = '', stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

export type Answer = (rest: string[]) => ReturnType<typeof ran>

/** $.fs.list over an in-memory file map: the folder's direct entries (a
 * deeper path makes its first part a dir); ENOENT when nothing is under it. */
export function listOf(files: Map<string, string>, path: string) {
  const dir = path.replace(/\/$/, '')
  const names = new Map<string, 'file' | 'dir'>()
  for (const p of files.keys()) {
    if (!p.startsWith(`${dir}/`)) continue
    const rest = p.slice(dir.length + 1)
    const i = rest.indexOf('/')
    if (i < 0) {
      if (!names.has(rest)) names.set(rest, 'file')
    } else {
      names.set(rest.slice(0, i), 'dir')
    }
  }
  if (!names.size) return { deny: 'ENOENT' }
  return { value: [...names].map(([name, kind]) => ({ name, kind, size: 0, mtimeMs: 0, isLink: false })) }
}

export function world(on: On, opts: { flagFails?: boolean; writeFails?: boolean; existsThrows?: boolean; envReady?: boolean; answers?: Record<string, Answer> } = {}): World {
  const w: World = {
    files: new Map(), runs: [], aborts: [], statuses: [], logs: [], registered: [], calls: [],
    release: new Map(), noresumeSeen: [], clock: mock.clock(on), submitted: [], events: [], opened: [], closed: [],
  }
  const env: Record<string, string> = { CHECKPOINT_HOME: HOME, HOME: '/home/u' }
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: ROOT }))
  on('session.root', () => ({ value: ROOT }))
  on('session.version', () => ({ value: { version: '2.1.301', base: '2.1.301', builtAt: '2026-10-15T12:58:20Z' } }))
  on('session.surfaces', () => ({ value: ['terminal'] as const }))
  on('fs.exists', ($, e) => {
    if (opts.existsThrows) throw new Error('fs is gone')
    return { value: w.files.has(e.path) }
  })
  on('fs.read', ($, e) => (w.files.has(e.path) ? { value: w.files.get(e.path)! } : { deny: 'ENOENT' }))
  on('fs.list', ($, e) => listOf(w.files, e.path))
  on('fs.write', ($, e) => {
    if (opts.writeFails) return { deny: 'EROFS' }
    w.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    w.runs.push(argv)
    w.events.push(`${argv[2]} ${argv[3]}`)
    const [, script, mode, ...rest] = argv
    expect(script).toEndWith('/hooks/env.sh')
    const ok = ran(0)
    if (mode === 'flag') {
      const [what, sid] = rest
      if (opts.flagFails) return ran(1, '', 'read-only')
      if (what === 'pause' && !w.files.has(`${HOME}/flags/${sid}`)) w.files.set(`${HOME}/flags/${sid}`, `2026-10-19T19:12:00+0200 ${rest[2]}`)
      if (what === 'now') w.files.set(`${HOME}/now/${sid}`, 'x')
      if (what === 'noresume') w.files.set(`${HOME}/noresume/${sid}`, 'x')
      if (what === 'clear-noresume') w.files.delete(`${HOME}/noresume/${sid}`)
      return ok
    }
    if (mode === 'run' && opts.answers?.[rest[0] ?? '']) return opts.answers[rest[0]!]!(rest)
    if (mode === 'doctor') return ran(0, 'pixi: /bin/pixi (0.81.0; in the tested range 0.68.0 to 0.82.0)\nruntime: ready\ngate: normal\n')
    if (mode === 'run' && rest[0] === 'status') return ran(0, 'Sessions:\n- sess-1  working/idle\n')
    if (mode === 'run' && rest[0] === 'watch-fire') {
      // Python's `pause now` killed the guarded call: it ends.
      w.release.get(rest[2] ?? '')?.()
      return ran(0, JSON.stringify({ killed: [{ command: 'make all', tool_use_id: rest[2], processes: 2, sigkill: true }], others: 0 }) + '\n')
    }
    return ok
  })
  if (opts.envReady !== false) {
    w.files.set(`${HOME}/rt/${SID}`, '/data/envs/abc123def456')
    w.files.set('/data/envs/abc123def456/.ready', '')
  }
  on('command.register', ($, e) => {
    w.registered.push(e)
    return { value: { command: e.name } }
  })
  on('turn.abort', ($, e) => {
    w.aborts.push(e.turnId)
    w.events.push('abort')
    // As Claude Code does: an aborted turn's running Bash call resolves at
    // once (it goes on as a background shell).
    for (const done of w.release.values()) done()
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.log', ($, e) => {
    w.logs.push(e.text)
    return { value: undefined }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('command.run', ($, e) => ({ text: `SKILL ${e.command} ${e.args}` }))
  on('prompt.submit', ($, e) => {
    w.noresumeSeen.push(w.files.has(`${HOME}/noresume/${SID}`))
    if (e.origin.kind === 'plugin') w.submitted.push(e.text)
    return { text: e.text }
  })
  on('ui.open', ($, e) => {
    w.opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.close', ($, e) => {
    w.closed.push(e.id)
    return { value: undefined }
  })
  on('classic.PreToolUse', () => ({}))
  on('tool.call', ($, e) => {
    w.calls.push(String(e.tool))
    const id = e.tool_use_id ?? ''
    if (id.startsWith('slow')) {
      return new Promise(resolve => w.release.set(id, () => resolve({ result: 'done' } as never)))
    }
    return { result: 'ok' } as never
  })
  return w
}

export const PRESENT = { isFullscreen: false, columns: 100 }
export const COMPOSER = { kind: 'composer' } as const

export async function start($: any) {
  await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true })
}

export async function settle(w: World, until: () => boolean) {
  for (let i = 0; i < 200 && !until(); i++) await w.clock.settle()
}

/** A denied call: `{ deny }`, or the errored result a session's hooks see. */
export function deniedText(r: { deny?: string; isError?: boolean; text?: string }): string | undefined {
  return r.deny ?? (r.isError ? String(r.text) : undefined)
}

