// checkpoint's mod and the journal (PLAN-v4 phase 2) under `claude plugin
// test`: the events it puts in the spool (env.sh spool), the ingest timer,
// /check-save, /check:save and /check-list, and the status line when
// recording fails. Everything beneath the mod is this file's, as in
// pause.test.ts: files in memory, hooks/env.sh answered here.
import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MockClock } from 'claude-code/testing'
import { footer, listOf } from './world.ts'

const HOME = '/state'
const SID = 'sess-1'
const ROOT = '/proj'
const COMPOSER = { kind: 'composer' } as const
const PRESENT = { isFullscreen: false, columns: 100 }

type Run = { argv: string[]; stdin?: string }
type World = { files: Map<string, string>; mtimes: Map<string, number>; runs: Run[]; aborts: string[]; statuses: (string | undefined)[]; clock: MockClock; modes: string[] }

function ran(exitCode: number, stdout = '', stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

function world(on: On, opts: { envReady?: boolean; saveFails?: boolean } = {}): World {
  const w: World = { files: new Map(), mtimes: new Map(), runs: [], aborts: [], statuses: [], clock: mock.clock(on), modes: [] }
  const env: Record<string, string> = { CHECKPOINT_HOME: HOME, HOME: '/home/u' }
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: ROOT }))
  on('session.root', () => ({ value: ROOT }))
  on('fs.exists', ($, e) => ({ value: w.files.has(e.path) }))
  on('fs.read', ($, e) => (w.files.has(e.path) ? { value: w.files.get(e.path)! } : { deny: 'ENOENT' }))
  on('fs.list', ($, e) => listOf(w.files, e.path))
  on('fs.write', ($, e) => {
    w.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.stat', ($, e) =>
    w.files.has(e.path)
      ? { value: { kind: 'file' as const, size: w.files.get(e.path)!.length, mtimeMs: w.mtimes.get(e.path) ?? 1, isLink: false } }
      : { deny: 'ENOENT' })
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    w.runs.push({ argv, stdin: e.init?.stdin })
    const [, script, mode, ...rest] = argv
    expect(script).toEndWith('/hooks/env.sh')
    if (mode === 'flag') {
      if (rest[0] === 'pause') w.files.set(`${HOME}/flags/${rest[1]}`, `2026-10-19T19:12:00+0200 ${rest[2]}`)
      return ran(0)
    }
    if (mode === 'run' && rest[0] === 'save') {
      return opts.saveFails
        ? ran(1, 'Checkpoint NOT saved: writing the journal failed (No space left on device).\n')
        : ran(0, 'Checkpoint c_0123 saved and synced to disk: task t_abc, journal seq 1-9, 1 file hash(es), summary 0123456789ab. Work goes on (nothing was paused).\n')
    }
    if (mode === 'run' && rest[0] === 'list') return ran(0, 'Checkpoints of task t_abc (9 journal records):\n- c_0123  2026-10-19 21:00:00  seq 1-9\n')
    return ran(0)
  })
  if (opts.envReady !== false) {
    w.files.set(`${HOME}/rt/${SID}`, '/data/envs/abc123def456')
    w.files.set('/data/envs/abc123def456/.ready', '')
  }
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('turn.abort', ($, e) => {
    w.aborts.push(e.turnId)
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.log', () => ({ value: undefined }))
  on('ui.render', { component: 'SessionMode' }, ($, e) => {
    w.modes = [...e.props.modes]
    return { type: 'Text' as const, props: { dimColor: true }, children: [e.props.modes.join(' & ')] }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: 'done' }))
  on('session.compact', ($, e) => (e.trigger === 'manual' ? { messages: e.messages } : { skip: 'nothing to compact' }))
  on('command.run', ($, e) => ({ text: `SKILL ${e.command} ${e.args}` }))
  on('tool.call', () => ({ result: 'ok' }) as never)
  return w
}

async function start($: any) {
  await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true })
}

async function settle(w: World, until: () => boolean) {
  for (let i = 0; i < 200 && !until(); i++) await w.clock.settle()
}

function spooled(w: World, event: string): Record<string, unknown>[] {
  return w.runs
    .filter(r => r.argv[2] === 'spool' && r.argv[3] === SID && r.argv[4] === event)
    .map(r => JSON.parse(r.stdin ?? '{}') as Record<string, unknown>)
}

test('turn.start and turn.complete go to the spool with the turn id and why it ended', async ($, on) => {
  const w = world(on)
  await start($)
  await $.turn.start({ text: 'build the widget', turnId: 't-1' })
  await $.turn.complete({ turnId: 't-1', reason: 'aborted', durationMs: 1234, isAborted: true, answer: '' } as never)
  await settle(w, () => spooled(w, 'mod.turn.complete').length > 0)
  const s = spooled(w, 'mod.turn.start')[0]!
  expect(s.turnId).toBe('t-1')
  expect(s.chars).toBe('build the widget'.length)
  expect(s.hook_event_name).toBe('mod.turn.start')
  expect(s.session_id).toBe(SID)
  expect(JSON.stringify(s)).not.toContain('build the widget') // the text itself comes from UserPromptSubmit, scrubbed
  const c = spooled(w, 'mod.turn.complete')[0]!
  expect(c.reason).toBe('aborted')
  expect(c.durationMs).toBe(1234)
})

test('a compaction is spooled at its start and its end (skipped or compacted)', async ($, on) => {
  const w = world(on)
  await start($)
  await $.session.compact({ trigger: 'auto', messages: [{ role: 'user', text: 'build the widget', toolUses: [] }] } as never)
  await settle(w, () => spooled(w, 'mod.compact').length >= 2)
  const ev = spooled(w, 'mod.compact')
  expect(ev[0]!.phase).toBe('start')
  expect(ev[0]!.trigger).toBe('auto')
  expect(ev[1]!.result).toBe('skipped')
})

test('a call the paused gate denies is spooled as denied, so it never reads as in flight', async ($, on) => {
  const w = world(on)
  await start($)
  w.files.set(`${HOME}/flags/${SID}`, '2026-10-19T19:12:00+0200 test')
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_9', command: 'make deploy', description: 'x' } as never)
  await settle(w, () => spooled(w, 'mod.tool.denied').length > 0)
  const d = spooled(w, 'mod.tool.denied')[0]!
  expect(d.tool_use_id).toBe('toolu_9')
  expect(d.tool).toBe('Bash')
  expect(d.why).toBe('paused')
})

test('the timer has Python ingest the spool only when its index changed', async ($, on) => {
  const w = world(on)
  await start($)
  const index = `${HOME}/spool/${SID}/index`
  const ingests = () => w.runs.filter(r => r.argv[2] === 'run' && r.argv[3] === 'ingest').length
  await w.clock.advance(20000)
  await settle(w, () => false)
  expect(ingests()).toBe(0) // no spool yet
  w.files.set(index, '1\tPreToolUse\t1.ev\t10\n')
  w.mtimes.set(index, 5000)
  await w.clock.advance(20000)
  await settle(w, () => ingests() > 0)
  expect(w.runs.find(r => r.argv[3] === 'ingest')!.argv.slice(2)).toEqual(['run', 'ingest', SID])
  await w.clock.advance(20000)
  await settle(w, () => false)
  expect(ingests()).toBe(1) // unchanged since
  w.mtimes.set(index, 9000)
  await w.clock.advance(20000)
  await settle(w, () => ingests() > 1)
  expect(ingests()).toBe(2)
})

test('/check-save mid-turn saves (Python, fsynced) and never stops the turn', async ($, on) => {
  const w = world(on)
  await start($)
  await $.turn.start({ text: 'go', turnId: 't-main' })
  const out = await $.command.run({ command: 'check-save', args: 'before the risky part', origin: COMPOSER, presentation: PRESENT })
  expect(w.runs.some(r => r.argv.slice(2).join(' ') === `run save ${SID} before the risky part`)).toBe(true)
  expect(out.text).toContain('Checkpoint c_0123 saved and synced to disk')
  expect(w.aborts).toEqual([])
  expect(w.files.has(`${HOME}/flags/${SID}`)).toBe(false)
})

test('/check-save says NOT saved when Python could not write, and without a runtime', async ($, on) => {
  const w = world(on, { saveFails: true })
  await start($)
  const out = await $.command.run({ command: 'check-save', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('NOT saved')
  expect(out.text).toContain('No space left on device')
})

test('/check-save without a ready runtime: NOT saved, and /check:save goes on to the skill', async ($, on) => {
  const w = world(on, { envReady: false })
  await start($)
  const out = await $.command.run({ command: 'check-save', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('NOT saved')
  const skill = await $.command.run({ command: 'check:save', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(skill.text).toBe('SKILL check:save ')
  expect(w.runs.some(r => r.argv[3] === 'save')).toBe(false)
})

test('/check:save is answered without a model turn when the runtime is ready; /check-list lists', async ($, on) => {
  const w = world(on)
  await start($)
  const out = await $.command.run({ command: 'check:save', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('saved and synced to disk')
  const list = await $.command.run({ command: 'check-list', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(list.text).toContain('Checkpoints of task t_abc')
  expect(w.runs.some(r => r.argv.slice(2).join(' ') === `run list ${SID}`)).toBe(true)
})

test('/check:status asks Python for this session (its task summary included)', async ($, on) => {
  const w = world(on)
  await start($)
  await $.command.run({ command: 'check:status', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(w.runs.some(r => r.argv.slice(2).join(' ') === `run status --session ${SID}`)).toBe(true)
})

test('the status line says when recording failed', async ($, on) => {
  const w = world(on)
  await start($)
  await settle(w, () => false)
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
  w.files.set(`${HOME}/journal-errors/${SID}`, '{"error": "No space left on device"}')
  await w.clock.advance(2000)
  await settle(w, () => w.statuses.length > 0)
  expect(w.statuses.at(-1)).toBe('recording FAILED · /check-doctor')
  expect(await footer($, w)).toEqual(['focus'])
  w.files.delete(`${HOME}/journal-errors/${SID}`)
  w.files.set(`${HOME}/spool-errors/${SID}`, 'x')
  await w.clock.advance(2000)
  await settle(w, () => false)
  expect(w.statuses.at(-1)).toBe('recording FAILED · /check-doctor')
})

test("an update's env still building: the previous env records, so the status line says recording (not first-run setup) and Python commands run", async ($, on) => {
  const w = world(on, { envReady: false })
  // SessionStart recorded the new version's env (not built yet); the previous one is.
  w.files.set(`${HOME}/rt/${SID}`, '/data/envs/newnew000000')
  w.files.set('/data/envs/newnew000000.building/pid', '123')
  w.files.set('/data/envs/old000000000/.ready', '')
  w.files.set('/data/envs/old000000000/.pixi/envs/default/bin/python3', '')
  await start($)
  await settle(w, () => false)
  expect(await footer($, w)).toEqual(['focus', 'check: recording · updating its runtime in the background'])
  expect(w.statuses).toEqual([])
  const out = await $.command.run({ command: 'check-save', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('saved and synced to disk')
  // Once the new env is ready: plain recording.
  w.files.set('/data/envs/newnew000000/.ready', '')
  await w.clock.advance(2000)
  await settle(w, () => false)
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
})

test('a first run (no env built at all): setting up, and Python commands say the runtime is not ready', async ($, on) => {
  const w = world(on, { envReady: false })
  w.files.set(`${HOME}/rt/${SID}`, '/data/envs/newnew000000')
  // a build folder and an env without .ready are not a usable older env
  w.files.set('/data/envs/newnew000000.building/pid', '123')
  w.files.set('/data/envs/half00000000/.pixi/envs/default/bin/python3', '')
  await start($)
  await settle(w, () => false)
  expect(await footer($, w)).toEqual(['focus', 'check: setting up its runtime (first run); pausing works'])
  expect(w.statuses).toEqual([])
  const out = await $.command.run({ command: 'check-save', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('NOT saved')
  expect(w.runs.some(r => r.argv[3] === 'save')).toBe(false)
})
