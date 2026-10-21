// checkpoint's mod under `claude plugin test` (tests/run_tests.py runs it).
// The kit's $ is the engine; everything beneath the mod is ./world.ts's: a
// file system in memory, hooks/env.sh simulated (flag writes, doctor), the
// session, the turn and the tool calls.
import { expect, test } from 'claude-code/testing'
import { COMPOSER, deniedText, FLAG, footer, HOME, PRESENT, ROOT, SID, settle, start, world } from './world.ts'
import { ackText, allowedTool, insideWorkDir, jobLive, parseSelfArgs, parseSelfPause, safeId, typedResumes } from '../../hooks/mod/logic.ts'

test('pure helpers agree with the Python side', async () => {
  expect(safeId('a/b c')).toBe('a_b_c')
  expect(safeId('')).toBe('_')
  expect(parseSelfPause('/check:pause')).toEqual({ now: false })
  expect(parseSelfPause(' /check:pause now ')).toEqual({ now: true })
  expect(parseSelfPause('Pause now!')).toEqual({ now: true })
  expect(parseSelfPause('pause all')).toBe(null)
  expect(parseSelfPause('/check:pause abc')).toBe(null)
  expect(parseSelfPause('please pause')).toBe(null)
  expect(parseSelfArgs('')).toEqual({ now: false })
  expect(parseSelfArgs('now')).toEqual({ now: true })
  expect(parseSelfArgs('all')).toBe(null)
  expect(allowedTool('Read')).toBe(true)
  expect(allowedTool('mcp__ccd_turn__wrap_up')).toBe(true)
  expect(allowedTool('mcp__plugin_check_jobs__job_start')).toBe(false)
  expect(allowedTool('Bash')).toBe(false)
  expect(insideWorkDir('/proj/.work/t/TRACKER.md', ['/proj'], '.work', '/proj')).toBe(true)
  expect(insideWorkDir('.work/t/TRACKER.md', ['/proj'], '.work', '/proj')).toBe(true)
  expect(insideWorkDir('/proj/.work/../src/x.py', ['/proj'], '.work', '/proj')).toBe(false)
  expect(insideWorkDir('/proj/.workshop/x', ['/proj'], '.work', '/proj')).toBe(false)
  expect(insideWorkDir('/proj/src/x.py', ['/proj'], '.work', '/proj')).toBe(false)
})

test('registers /check-pause, /check-doctor, /check-save, /check-list, /check-resume, /check-cancel, /check-stop and /check-diff as immediate commands, and shows recording', async ($, on) => {
  const w = world(on)
  await start($)
  expect(w.registered.map(r => [r.name, r.immediate])).toEqual([['check-pause', true], ['check-doctor', true], ['check-save', true], ['check-list', true],
    ['check-resume', true], ['check-cancel', true], ['check-stop', true], ['check-diff', true]])
  await settle(w, () => false)
  // A normal state: the footer's dim mode label, not the status line (its warning sign).
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
  expect(w.statuses).toEqual([])
})

test('/check-pause mid-turn: flag written, turn aborted, ack says what still runs, next calls denied', async ($, on) => {
  const w = world(on)
  await start($)
  await $.turn.start({ text: 'go', turnId: 't-main' })
  const running = $.tool.call({ tool: 'Bash', tool_use_id: 'slow-1', command: 'sleep 31.5', description: 'wait' } as never)
  await settle(w, () => w.calls.includes('Bash'))
  const out = await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(w.files.has(FLAG)).toBe(true)
  expect(w.runs.some(r => r.slice(2).join(' ') === `flag pause ${SID} /check-pause`)).toBe(true)
  expect(w.aborts).toEqual(['t-main'])
  expect(out.text).toContain('checkpoint: paused this session (at once')
  expect(out.text).toContain('Stopped the running turn.')
  expect(out.text).toContain('Still running: Bash `sleep 31.5`, left running')
  expect(out.text).toContain('Tool calls stay held until you resume')
  w.release.get('slow-1')!()
  await running
  const denied = await $.tool.call({ tool: 'Bash', tool_use_id: 'b2', command: 'echo second > second.txt', description: 'x' } as never)
  expect(deniedText(denied as never)).toContain('Paused by the user (checkpoint)')
  expect(w.calls.filter(c => c === 'Bash')).toHaveLength(1)
  // what the Python gate allows while paused stays allowed
  await $.tool.call({ tool: 'Read', tool_use_id: 'r1', file_path: '/proj/a.py' } as never)
  expect(w.calls).toContain('Read')
  await $.tool.call({ tool: 'Write', tool_use_id: 'w1', file_path: '/proj/.work/t/TRACKER.md', content: 'x' } as never)
  expect(w.calls).toContain('Write')
  const w2 = await $.tool.call({ tool: 'Write', tool_use_id: 'w2', file_path: '/proj/src/a.py', content: 'x' } as never)
  expect(deniedText(w2 as never)).toContain('Paused by the user')
  // the Python side records it in the background (session record + checkpoint)
  await settle(w, () => w.runs.some(r => r[2] === 'run' && r[3] === 'sync-pause'))
  expect(w.runs.some(r => r.slice(2).join(' ') === `run sync-pause ${SID}`)).toBe(true)
  await settle(w, () => w.statuses.at(-1)?.startsWith('paused') === true)
  expect(w.statuses.at(-1)).toContain('paused')
})

test('/check-pause now: the running command is stopped first (Python), then the turn ends', async ($, on) => {
  const w = world(on)
  await start($)
  await $.turn.start({ text: 'go', turnId: 't-main' })
  const running = $.tool.call({ tool: 'Bash', tool_use_id: 'slow-2', command: 'make all', description: 'build' } as never)
  await settle(w, () => w.calls.includes('Bash'))
  const out = await $.command.run({ command: 'check-pause', args: 'now', origin: COMPOSER, presentation: PRESENT })
  expect(w.files.has(`${HOME}/now/${SID}`)).toBe(true)
  expect(w.runs.some(r => r.slice(2).join(' ') === `run watch-fire ${SID} slow-2`)).toBe(true)
  const fired = w.runs.findIndex(r => r[2] === 'run' && r[3] === 'watch-fire')
  expect(fired).toBeGreaterThan(-1)
  expect(w.aborts).toEqual(['t-main'])
  expect(out.text).toContain('Stopped Bash `make all` (pause now: 2 process(es) ended: SIGTERM, then SIGKILL for what was left).')
  expect(out.text).not.toContain('left running')
  await running
})

test('/check-pause now without a ready runtime: ends the turn at once and says the command keeps running', async ($, on) => {
  const w = world(on, { envReady: false })
  await start($)
  await $.turn.start({ text: 'go', turnId: 't-main' })
  const running = $.tool.call({ tool: 'Bash', tool_use_id: 'slow-3', command: 'sleep 9', description: 'x' } as never)
  await settle(w, () => w.calls.includes('Bash'))
  const out = await $.command.run({ command: 'check-pause', args: 'now', origin: COMPOSER, presentation: PRESENT })
  expect(w.aborts).toEqual(['t-main'])
  expect(out.text).toContain("`now` couldn't stop it: checkpoint's runtime isn't ready.")
  expect(out.text).toContain('left running')
  w.release.get('slow-3')!()
  await running
})

test('/check:pause is answered without a model turn; all and <session> go on to the skill', async ($, on) => {
  const w = world(on)
  await start($)
  const mine = await $.command.run({ command: 'check:pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(mine.text).toContain('paused this session')
  expect(mine.text).toContain('No turn was running.')
  const again = await $.command.run({ command: 'check:pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(again.text).toContain('already paused (since 19:12:00)')
  const all = await $.command.run({ command: 'check:pause', args: 'all', origin: COMPOSER, presentation: PRESENT })
  expect(all.text).toBe('SKILL check:pause all')
})

test('the gate fails closed: a check that throws denies', async ($, on) => {
  const w = world(on, { existsThrows: true })
  await start($)
  const r = await $.tool.call({ tool: 'Bash', tool_use_id: 'b9', command: 'ls', description: 'x' } as never)
  expect(deniedText(r as never)).toContain('checking whether this session is paused failed')
  expect(w.calls).toEqual([])
})

test('a flag that cannot be written still holds the session (in memory) and says so', async ($, on) => {
  const w = world(on, { flagFails: true, writeFails: true })
  await start($)
  const out = await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain("Couldn't record the pause on disk")
  const r = await $.tool.call({ tool: 'Bash', tool_use_id: 'b3', command: 'ls', description: 'x' } as never)
  expect(deniedText(r as never)).toContain('Paused by the user')
  expect(w.calls).toEqual([])
})

test('no self-resume: a prompt that is not the person\'s is marked noresume for the classic hook', async ($, on) => {
  const w = world(on)
  await start($)
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  await $.prompt.submit({ text: 'scheduled check', isMeta: true, origin: { kind: 'scheduled-trigger' }, wait: false })
  expect(w.noresumeSeen.at(-1)).toBe(true)
  expect(w.files.has(`${HOME}/noresume/${SID}`)).toBe(false)
  await $.prompt.submit({ text: 'loop tick', origin: { kind: 'task-notification' }, wait: false })
  expect(w.noresumeSeen.at(-1)).toBe(true)
  await $.prompt.submit({ text: 'go on', origin: COMPOSER, wait: false })
  expect(w.noresumeSeen.at(-1)).toBe(false)
})

test('no self-resume for slash commands either: a schedule, /loop, peer or channel firing one is marked noresume', async ($, on) => {
  const w = world(on)
  await start($)
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  const fired = [
    { origin: { kind: 'scheduled-trigger' } },              // a scheduled task or /loop
    { origin: { kind: 'peer' } },
    { origin: { kind: 'channel', server: 'slack' } },
    { origin: { kind: 'unclassified' } },
    { origin: COMPOSER, isMeta: true },                      // composer, but not seen as typed
  ]
  for (const f of fired) {
    const r = await $.prompt.submit({ text: '/vfy-skill', command: { name: 'vfy-skill', args: '' }, wait: false, ...f } as never)
    expect(r.drop).toBeUndefined()
    expect(w.noresumeSeen.at(-1)).toBe(true)
    expect(w.files.has(`${HOME}/noresume/${SID}`)).toBe(false) // cleared after the classic hook ran
  }
  await $.prompt.submit({ text: '/check:resume', command: { name: 'check:resume', args: '' }, origin: { kind: 'scheduled-trigger' }, wait: false } as never)
  expect(w.noresumeSeen.at(-1)).toBe(true)
  expect(w.noresumeSeen).toHaveLength(fired.length + 1)
  // the person's own slash command isn't marked: it resumes, as a typed message does
  await $.prompt.submit({ text: '/vfy-skill', command: { name: 'vfy-skill', args: '' }, origin: COMPOSER, wait: false } as never)
  expect(w.noresumeSeen.at(-1)).toBe(false)
  // not paused: nothing is marked
  w.files.delete(FLAG)
  await $.prompt.submit({ text: '/vfy-skill', command: { name: 'vfy-skill', args: '' }, origin: { kind: 'scheduled-trigger' }, wait: false } as never)
  expect(w.noresumeSeen.at(-1)).toBe(false)
  expect(w.runs.filter(r => r[2] === 'flag' && r[3] === 'noresume')).toHaveLength(fired.length + 1)
})

test('a noresume mark env.sh cannot write is written by the mod itself', async ($, on) => {
  const w = world(on, { flagFails: true })
  await start($)
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  const r = await $.prompt.submit({ text: 'scheduled check', isMeta: true, origin: { kind: 'scheduled-trigger' }, wait: false })
  expect(r.drop).toBeUndefined()
  expect(w.noresumeSeen).toEqual([true])
  expect(w.files.get(`${HOME}/noresume/${SID}`)).toContain(' mod')
})

test('a noresume mark nothing can write holds the prompt back (fails closed), never a typed one', async ($, on) => {
  const w = world(on, { flagFails: true, writeFails: true })
  await start($)
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  const r = await $.prompt.submit({ text: 'scheduled check', isMeta: true, origin: { kind: 'scheduled-trigger' }, wait: false })
  expect(r.drop).toContain("held back a message that isn't yours")
  const c = await $.prompt.submit({ text: '/vfy-skill', command: { name: 'vfy-skill', args: '' }, origin: { kind: 'scheduled-trigger' }, wait: false } as never)
  expect(c.drop).toContain("held back a message that isn't yours")
  expect(w.noresumeSeen).toEqual([]) // the classic hook never saw them
  const mine = await $.prompt.submit({ text: 'go on', origin: COMPOSER, wait: false })
  expect(mine.drop).toBeUndefined()
  expect(w.noresumeSeen).toEqual([false])
})

test('a pause held only in memory: lifted by a typed /check:resume or message, not by /check:status, pause all or a schedule', async ($, on) => {
  const w = world(on, { flagFails: true, writeFails: true })
  await start($)
  const out = await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain("Couldn't record the pause on disk")
  expect(out.text).toContain('/check:resume')
  const bash = async (id: string) => deniedText(await $.tool.call({ tool: 'Bash', tool_use_id: id, command: 'ls', description: 'x' } as never) as never)
  await $.prompt.submit({ text: '/check:status', command: { name: 'check:status', args: '' }, origin: COMPOSER, wait: false } as never)
  await $.prompt.submit({ text: 'pause all', origin: COMPOSER, wait: false })
  await $.prompt.submit({ text: '/vfy-skill', command: { name: 'vfy-skill', args: '' }, origin: { kind: 'scheduled-trigger' }, wait: false } as never)
  expect(await bash('m1')).toContain('Paused by the user')
  await $.prompt.submit({ text: '/check:resume', command: { name: 'check:resume', args: '' }, origin: COMPOSER, wait: false } as never)
  expect(await bash('m2')).toBeUndefined()
  expect(w.logs.at(-1)).toContain('resumed this session (its pause was held in memory only')
  // and a typed message lifts it too
  await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(await bash('m3')).toContain('Paused by the user')
  await $.prompt.submit({ text: 'please pause the video and go on', origin: COMPOSER, wait: false })
  expect(await bash('m4')).toBeUndefined()
  expect(w.calls.filter(c => c === 'Bash')).toHaveLength(2)
})

test('typedResumes follows hooks.handle_prompt', async () => {
  expect(typedResumes('go on')).toBe(true)
  expect(typedResumes('pause')).toBe(false)
  expect(typedResumes('Pause all now.')).toBe(false)
  expect(typedResumes('pause the video')).toBe(true)
  expect(typedResumes('/check:resume')).toBe(true)
  expect(typedResumes('/check:resume all')).toBe(true)
  expect(typedResumes('/check:status')).toBe(false)
  expect(typedResumes('/check:pause now')).toBe(false)
  expect(typedResumes('/check:help')).toBe(false)
  expect(typedResumes('/check:resume', { name: 'check:resume' })).toBe(true)
  expect(typedResumes('/check:doctor', { name: 'check:doctor' })).toBe(false)
  expect(typedResumes('/check-pause', { name: 'check-pause' })).toBe(false)
  expect(typedResumes('/review', { name: 'review' })).toBe(true)
})

test('the typed keyword `pause` goes on to the classic hook (never dropped back into the prompt box)', async ($, on) => {
  const w = world(on)
  await start($)
  const r = await $.prompt.submit({ text: 'pause', origin: COMPOSER, wait: false })
  expect(r.drop).toBeUndefined()
  expect(r.text).toBe('pause')
  expect(w.noresumeSeen).toEqual([false])
})

test('`pause` typed while a turn runs pauses at once (prompt.submit with the turn\'s id), then goes on', async ($, on) => {
  const w = world(on)
  await start($)
  await $.turn.start({ text: 'go', turnId: 't-q' })
  const r = await $.prompt.submit({ text: 'pause', origin: COMPOSER, wait: false, turnId: 't-q' })
  expect(w.files.has(FLAG)).toBe(true)
  expect(w.aborts).toEqual(['t-q'])
  expect(w.logs.at(-1)).toContain('paused this session')
  expect(r.text).toBe('pause')
  const later = await $.prompt.submit({ text: 'please pause the video', origin: COMPOSER, wait: false, turnId: 't-q' })
  expect(later.text).toBe('please pause the video')
  expect(w.aborts).toEqual(['t-q'])
})

test('/check-doctor reports Claude Code, surfaces and the runtime half', async ($, on) => {
  const w = world(on)
  await start($)
  const r = await $.command.run({ command: 'check-doctor', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(r.text).toContain('Claude Code: 2.1.301')
  expect(r.text).toContain('surfaces: terminal')
  expect(r.text).toContain('- runtime: ready')
  expect(r.text).toContain('- gate: normal')
  expect(w.runs.some(r => r[2] === 'doctor' && r[3] === SID)).toBe(true)
  const s = await $.command.run({ command: 'check:status', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(s.text).toContain('Sessions:')
})

test('ack text names jobs and other calls', async () => {
  const t = ackText({ already: false, wrote: true, turn: 'Stopped the running turn.', calls: [{ id: 'a1', tool: 'Agent', started: 0 }], stopped: [], jobs: [{ id: 'train-1', name: 'train' }], tookMs: 30 })
  expect(t).toContain('in 0.03 s')
  expect(t).toContain('Also in flight: Agent')
  expect(t).toContain("Detached jobs keep running (checkpoint's runner): train (train-1)")
})

test("ack text: the gateway's pause outcomes when gw-pause answered, else its jobs named as not told", async () => {
  const base = { already: false, wrote: true, turn: 'Stopped the running turn.', calls: [], stopped: [], tookMs: 20 }
  const told = ackText({ ...base, jobs: [{ id: 'train-1', name: 'train', supervisor: 'gateway' }],
    gateway: 'Gateway jobs of this task: train (train-1, j_X), on_pause interrupt: interrupted: exited (signal INT).' })
  expect(told).toContain('on_pause interrupt: interrupted: exited (signal INT)')
  expect(told).not.toContain('Detached jobs keep running')
  expect(told).not.toContain('not told of the pause')
  const untold = ackText({ ...base, jobs: [{ id: 'train-1', name: 'train', supervisor: 'gateway' }] })
  expect(untold).toContain("Gateway jobs, not told of the pause (their pause policies didn't apply): train (train-1)")
})

test('jobLive follows jobs.is_live: a gateway job whose outcome is unknown (gone or not) may still run', async () => {
  expect(jobLive({ owner: 'gateway', state: 'unknown', gone: true })).toBe(true)
  expect(jobLive({ owner: 'gateway', state: 'unknown' })).toBe(true)
  expect(jobLive({ owner: 'gateway', state: 'waiting_auth' })).toBe(true)
  expect(jobLive({ owner: 'gateway', state: 'suspended' })).toBe(true)
  for (const st of ['exited', 'failed', 'canceled']) expect(jobLive({ owner: 'gateway', state: st })).toBe(false)
  expect(jobLive({ state: 'running' })).toBe(true)
  expect(jobLive({ state: 'stop_failed' })).toBe(true)
  for (const st of ['exited', 'killed', 'vanished']) expect(jobLive({ state: st })).toBe(false)
})
