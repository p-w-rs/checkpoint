// checkpoint's recovery controls in the mod (PLAN-v4 phase 3) under `claude
// plugin test`: /check-resume, /check-cancel, /check-stop, /check-diff, the
// Checkpoints pane, and what stays usable while paused. The world beneath
// the mod is ./world.ts (hooks/env.sh simulated; `answers` are Python's).
import { expect, test } from 'claude-code/testing'
import { COMPOSER, deniedText, FLAG, HOME, PRESENT, ran, settle, start, world } from './world.ts'
import { ackText, parseCancelArgs, parseSelfArgs, parseSelfPause, RESUME_PROMPT, rowText, typedResumes } from '../../hooks/mod/logic.ts'

const ROWS = {
  task: 't_1', checkpoints: [
    { id: 'c_old', time: '2026-10-20 10:00:00', seq_from: 1, seq_to: 9, line: '1/2 steps done; 1 file(s)', staleness: 'stale', stale_why: ['1 file(s) changed since'] },
    { id: 'c_new', time: '2026-10-20 11:00:00', seq_from: 1, seq_to: 20, line: '2/2 steps done; 2 file(s)', staleness: 'current', newest: true },
  ],
}

function answers(log: string[]) {
  return {
    'resume-now': (rest: string[]) => {
      log.push(`resume-now ${rest.slice(2).join(' ')}`.trim())
      return ran(0, 'Resumed: Reconciled task t_1 against checkpoint c_new: 1 file(s) changed outside the task (kept, nothing overwritten).\n')
    },
    cancel: (rest: string[]) => {
      log.push(`cancel ${rest.slice(2).join(' ')}`.trim())
      return ran(0, 'Task t_1 canceled; its history stays in the journal. Job train (train-1): stopped (SIGTERM; SIGKILL after the grace period).\n')
    },
    'stop-job': (rest: string[]) => {
      log.push(`stop-job ${rest.slice(2).join(' ')}`)
      return ran(0, 'Job train (train-1) stopped: its process group got SIGTERM and is gone.\n')
    },
    'diff-session': (rest: string[]) => {
      log.push(`diff ${rest.slice(2).join(' ')}`)
      return ran(0, 'Diff of task t_1: checkpoint c_old (seq 9) -> now (seq 30)\n## Files\n- /proj/a.py: 111 -> 222\n')
    },
    'list-json': () => ran(0, JSON.stringify(ROWS)),
    list: () => ran(0, 'Checkpoints of task t_1 (30 journal records):\n- c_old ...\n- c_new ...\n'),
  }
}

test('pure helpers: interrupt is now, the cancel policy, rows, the new commands never resume', async () => {
  expect(parseSelfArgs('interrupt')).toEqual({ now: true })
  expect(parseSelfPause('pause interrupt')).toEqual({ now: true })
  expect(parseSelfPause('/check:pause interrupt')).toEqual({ now: true })
  expect(parseCancelArgs('')).toEqual({ policy: '', note: '' })
  expect(parseCancelArgs('detach no longer needed')).toEqual({ policy: 'detach', note: 'no longer needed' })
  expect(parseCancelArgs('interrupt')).toEqual({ policy: 'interrupt', note: '' })
  for (const c of ['list', 'diff c_1', 'cancel', 'stop train', 'complete', 'export last', 'save', 'forget t_1']) {
    expect(typedResumes(`/check:${c}`)).toBe(false)
  }
  expect(typedResumes('/check:resume c_1')).toBe(true)
  expect(typedResumes('pause interrupt')).toBe(false)
  expect(typedResumes('/check-resume', { name: 'check-resume' })).toBe(false) // the mod's own command does it
  expect(rowText(ROWS.checkpoints[0]!)).toContain('[stale: 1 file(s) changed since]')
  expect(rowText(ROWS.checkpoints[1]!)).toContain('[current] (newest)')
  const t = ackText({ already: false, wrote: true, turn: 'x', calls: [], stopped: [{ id: 'b', tool: 'Bash', command: 'make', started: 0, detail: '3 process(es) ended: SIGTERM' }], jobs: [], tookMs: 5 })
  expect(t).toContain('Stopped Bash `make` (pause now: 3 process(es) ended: SIGTERM).')
})

test('/check-resume while paused: Python reconciles and lifts the pause, then a prompt goes in so the task goes on', async ($, on) => {
  const log: string[] = []
  const w = world(on, { answers: answers(log) })
  await start($)
  await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(w.files.has(FLAG)).toBe(true)
  // the resume command lifts it (Python's resume-now clears the flag on disk)
  w.files.delete(FLAG)
  const out = await $.command.run({ command: 'check-resume', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(log).toEqual(['resume-now'])
  expect(out.text).toContain('Resumed: Reconciled task t_1 against checkpoint c_new')
  expect(out.text).toContain('The task goes on now')
  await w.clock.advance(300)   // the session.start timer sends it
  await settle(w, () => w.submitted.length > 0)
  expect(w.submitted).toEqual([RESUME_PROMPT])
  // the submitted prompt isn't marked noresume (the session isn't paused any more)
  expect(w.noresumeSeen.at(-1)).toBe(false)
})

test('/check-resume <checkpoint> passes the checkpoint; not paused and no checkpoint: nothing is sent', async ($, on) => {
  const log: string[] = []
  const a = answers(log)
  const w = world(on, {
    answers: {
      ...a,
      'resume-now': (rest: string[]) => {
        log.push(`resume-now ${rest.slice(2).join(' ')}`.trim())
        return rest.length > 2 ? ran(0, 'Resuming: Reconciled task t_1 against checkpoint c_old (saved ...).\n')
          : ran(0, "This session wasn't paused. Reconciled task t_1 against checkpoint c_new: nothing changed.\n")
      },
    },
  })
  await start($)
  const idle = await $.command.run({ command: 'check-resume', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(idle.text).toContain("wasn't paused")
  await w.clock.advance(1000)
  expect(w.submitted).toEqual([])
  const pick = await $.command.run({ command: 'check-resume', args: 'c_old', origin: COMPOSER, presentation: PRESENT })
  expect(log.at(-1)).toBe('resume-now c_old')
  expect(pick.text).toContain('against checkpoint c_old')
  await w.clock.advance(300)
  await settle(w, () => w.submitted.length > 0)
  expect(w.submitted).toEqual([RESUME_PROMPT])
})

test('/check-resume without a ready runtime refuses to resume unreconciled (a typed message still resumes)', async ($, on) => {
  const w = world(on, { envReady: false })
  await start($)
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  const out = await $.command.run({ command: 'check-resume', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('reconciles first')
  expect(w.files.has(FLAG)).toBe(true)
  expect(w.submitted).toEqual([])
})

test('/check-cancel: the running command is stopped (Python) before the turn ends; detach ends the turn first', async ($, on) => {
  const log: string[] = []
  const w = world(on, { answers: answers(log) })
  await start($)
  await $.turn.start({ text: 'go', turnId: 't-c' })
  const out = await $.command.run({ command: 'check-cancel', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(log).toEqual(['cancel'])
  expect(w.aborts).toEqual(['t-c'])
  expect(out.text).toContain('Task t_1 canceled')
  expect(out.text).toContain('Stopped the running turn.')
  expect(w.events.indexOf('run cancel')).toBeLessThan(w.events.indexOf('abort'))
  // detach: the turn ends first, then Python applies the policy
  w.events.length = 0
  await $.turn.start({ text: 'again', turnId: 't-d' })
  const det = await $.command.run({ command: 'check-cancel', args: 'detach', origin: COMPOSER, presentation: PRESENT })
  expect(log.at(-1)).toBe('cancel detach')
  expect(w.aborts).toEqual(['t-c', 't-d'])
  expect(w.events.indexOf('abort')).toBeLessThan(w.events.indexOf('run cancel'))
  expect(det.text).toContain('canceled')
})

test('/check-cancel holds every tool call while Python stops the work, and lets go once it is done', async ($, on) => {
  const log: string[] = []
  let release: (() => void) | undefined
  const w = world(on, {
    answers: {
      ...answers(log),
      // Python's cancel takes a while (jobs that ignore SIGTERM): it answers when released.
      cancel: () => new Promise(resolve => {
        release = () => resolve(ran(0, 'Task t_1 canceled; its history stays in the journal. Job w0 (w0-1): stopped (SIGTERM; SIGKILL after the grace period).\n'))
      }) as never,
    },
  })
  await start($)
  await $.turn.start({ text: 'run the workers', turnId: 't-h' })
  const pending = $.command.run({ command: 'check-cancel', args: '', origin: COMPOSER, presentation: PRESENT })
  await settle(w, () => release !== undefined)
  expect(release).toBeDefined()
  // Meanwhile the model asks to start another job, and a subagent to run a command: both held.
  const js = await $.tool.call({ tool: 'mcp__plugin_check_jobs__job_start', tool_use_id: 'js1', name: 'w2', command: 'sleep 9' } as never) as { deny?: string; isError?: boolean; text?: string }
  expect(deniedText(js)).toContain('canceling this task')
  const sub = await $.tool.call({ tool: 'Bash', tool_use_id: 'sb1', command: 'make', description: 'x', agentId: 'a1' } as never) as { deny?: string; isError?: boolean; text?: string }
  expect(deniedText(sub)).toContain('canceling this task')
  expect(w.calls).not.toContain('mcp__plugin_check_jobs__job_start')
  expect(w.files.has(FLAG)).toBe(false) // held in memory, not a pause
  release!()
  const out = await pending
  expect(out.text).toContain('Task t_1 canceled')
  expect(w.aborts).toEqual(['t-h'])
  // Done: the next session's calls go ahead.
  const after = await $.tool.call({ tool: 'Bash', tool_use_id: 'b2', command: 'ls', description: 'x' } as never) as { deny?: string; isError?: boolean; text?: string }
  expect(deniedText(after)).toBeUndefined()
  expect(w.calls).toContain('Bash')
})

test('/check-stop and /check-diff answer at once from Python, also while paused, and resume nothing', async ($, on) => {
  const log: string[] = []
  const w = world(on, { answers: answers(log) })
  await start($)
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  const st = await $.command.run({ command: 'check-stop', args: 'train', origin: COMPOSER, presentation: PRESENT })
  expect(st.text).toContain('Job train (train-1) stopped')
  expect(log).toContain(`stop-job train`)
  const df = await $.command.run({ command: 'check-diff', args: 'c_old now', origin: COMPOSER, presentation: PRESENT })
  expect(df.text).toContain('## Files')
  expect(log).toContain('diff c_old now')
  expect(w.files.has(FLAG)).toBe(true)        // still paused
  expect(w.submitted).toEqual([])
  // the tool gate still holds while paused
  const r = await $.tool.call({ tool: 'Bash', tool_use_id: 'b1', command: 'ls', description: 'x' } as never) as { deny?: string; isError?: boolean; text?: string }
  expect(r.deny ?? (r.isError ? String(r.text) : undefined)).toContain('Paused by the user')
})

test('/check-stop: one "checkpoint:" prefix, whether Python answered, failed, or no runtime is ready', async ($, on) => {
  const log: string[] = []
  const w = world(on, { answers: { ...answers(log), 'stop-job': () => ran(1, '') } })
  await start($)
  const failed = await $.command.run({ command: 'check-stop', args: 'train', origin: COMPOSER, presentation: PRESENT })
  expect(failed.text).toBe('checkpoint: stop-job failed (exit 1); /check-doctor says why.')
  void w
})

test('/check-stop without a ready runtime: said once, not "checkpoint: checkpoint:"', async ($, on) => {
  world(on, { envReady: false })
  await start($)
  const out = await $.command.run({ command: 'check-stop', args: 'train', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toBe("checkpoint: checkpoint's runtime isn't ready yet (first-time setup).")
  expect(out.text).not.toContain('checkpoint: checkpoint:')
})

test('/check-stop names gateway jobs in its description, and an answer from Python gets the prefix once', async ($, on) => {
  const log: string[] = []
  const w = world(on, { answers: answers(log) })
  await start($)
  const reg = w.registered.find(r => r.name === 'check-stop') as { description?: string } | undefined
  expect(reg?.description).toContain('gateway job')
  const st = await $.command.run({ command: 'check-stop', args: 'train', origin: COMPOSER, presentation: PRESENT })
  expect(st.text).toBe('checkpoint: Job train (train-1) stopped: its process group got SIGTERM and is gone.')
})

test('/check-list opens the Checkpoints pane: rows with their staleness, diff with now, resume from here', async ($, on) => {
  const log: string[] = []
  const w = world(on, { answers: answers(log) })
  await start($)
  const out = await $.command.run({ command: 'check-list', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain('Checkpoints of task t_1')
  await settle(w, () => w.opened.length > 0)
  expect(w.opened).toEqual(['check-checkpoints'])
  const ui = await $.ui.mount({
    plugin: 'check', surface: 'terminal', component: 'Pane', requestId: 'check-checkpoints',
    props: { title: 'Checkpoints', isFocused: true, bodyColumns: 100, placement: 'dock', scroll: {} as never, view: {} as never },
  })
  expect((await ui.find({ type: 'Text', text: /c_old .*\[stale: 1 file\(s\) changed since\]/ }))).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /c_new .*\[current\] \(newest\)/ }))).toBeDefined()
  await ui.press({ key: 'diff-c_old' })
  expect(log).toContain('diff c_old now')
  expect(await ui.find({ type: 'Text', text: /\/proj\/a\.py: 111 -> 222/ })).toBeDefined()
  w.files.set(FLAG, '2026-10-19T19:12:00+0200 test')
  await ui.press({ key: 'resume-c_old' })
  expect(log).toContain('resume-now c_old')
  await w.clock.advance(300)
  await settle(w, () => w.submitted.length > 0)
  expect(w.submitted).toEqual([RESUME_PROMPT])
  await ui.press({ key: 'close' })
  expect(w.closed).toEqual(['check-checkpoints'])
  await ui.unmount()
})

test('without a ready runtime /check-list says so and opens no pane', async ($, on) => {
  const w = world(on, { envReady: false })
  await start($)
  const out = await $.command.run({ command: 'check-list', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(out.text).toContain("runtime isn't ready")
  await settle(w, () => false)
  expect(w.opened).toEqual([])
  void HOME
})
