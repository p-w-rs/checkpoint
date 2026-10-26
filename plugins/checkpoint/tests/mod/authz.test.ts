// checkpoint's mod and authorization references (0.7.0) under `claude plugin
// test`: the tool.check observer spools Claude Code's permission verdict for
// real calls a rule, a hook or an ask decided (and leaves the verdict as it
// is), and the gate spools what came of an ask or a deny. Everything beneath
// the mod is this file's: files in memory, hooks/env.sh answered here, the
// verdict beneath answered by the test.
import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MockClock } from 'claude-code/testing'

const HOME = '/state'
const SID = 'sess-1'
const ROOT = '/proj'

type Run = { argv: string[]; stdin?: string }
type Verdict = { decision: 'allow' | 'ask' | 'deny'; reason?: string; rule?: string; hook?: string }
type World = { files: Map<string, string>; runs: Run[]; clock: MockClock; verdict: Verdict; answer: Record<string, unknown>; spoolFails: boolean }

function ran(exitCode: number, stdout = '', stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

function world(on: On): World {
  const w: World = {
    files: new Map(), runs: [], clock: mock.clock(on), verdict: { decision: 'allow' },
    answer: { result: 'ok' }, spoolFails: false,
  }
  const env: Record<string, string> = { CHECKPOINT_HOME: HOME, HOME: '/home/u' }
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: ROOT }))
  on('session.root', () => ({ value: ROOT }))
  on('fs.exists', ($, e) => ({ value: w.files.has(e.path) }))
  on('fs.read', ($, e) => (w.files.has(e.path) ? { value: w.files.get(e.path)! } : { deny: 'ENOENT' }))
  on('fs.write', ($, e) => {
    w.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.stat', () => ({ deny: 'ENOENT' }))
  on('process.run', ($, e) => {
    w.runs.push({ argv: [...e.argv], stdin: e.init?.stdin })
    expect(e.argv[1]).toEndWith('/hooks/env.sh')
    if (w.spoolFails && e.argv[2] === 'spool') throw new Error('no shell')
    return ran(0)
  })
  w.files.set(`${HOME}/rt/${SID}`, '/data/envs/abc123def456')
  w.files.set('/data/envs/abc123def456/.ready', '')
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('tool.check', () => w.verdict as never)
  on('tool.call', () => w.answer as never)
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

const GW_ASK = 'gateway: ssh on build is set to "ask"; approve it to run.'

test('a verdict a hook decided (the gateway asks) goes to the spool and comes back unchanged', async ($, on) => {
  const w = world(on)
  await start($)
  w.verdict = { decision: 'ask', reason: GW_ASK, hook: 'PreToolUse' }
  const v = await $.tool.check({ tool: 'Bash', input: { command: 'ssh build make' }, tool_use_id: 'toolu_1' } as never)
  expect(v.decision).toBe('ask')
  expect(v.reason).toBe(GW_ASK)
  await settle(w, () => spooled(w, 'mod.tool.check').length > 0)
  const s = spooled(w, 'mod.tool.check')[0]!
  expect(s.tool_use_id).toBe('toolu_1')
  expect(s.decision).toBe('ask')
  expect(s.hook).toBe('PreToolUse')
  expect(s.reason).toBe(GW_ASK) // to the spool only: Python keeps the right's name, never the text
  expect(JSON.stringify(s)).not.toContain('ssh build make') // the input never goes
})

test('a rule\'s allow is a reference; a plain allow, or a query without a tool_use_id, is not', async ($, on) => {
  const w = world(on)
  await start($)
  w.verdict = { decision: 'allow' }
  await $.tool.check({ tool: 'Read', input: { file_path: '/proj/a' }, tool_use_id: 'toolu_2' } as never)
  w.verdict = { decision: 'deny', reason: 'no' }
  await $.tool.check({ tool: 'Bash', input: { command: 'x' } } as never)
  w.verdict = { decision: 'allow', rule: 'Bash(git push:*)' }
  await $.tool.check({ tool: 'Bash', input: { command: 'git push' }, tool_use_id: 'toolu_3' } as never)
  await settle(w, () => spooled(w, 'mod.tool.check').length > 0)
  await settle(w, () => false)
  const s = spooled(w, 'mod.tool.check')
  expect(s.length).toBe(1)
  expect(s[0]!.tool_use_id).toBe('toolu_3')
  expect(s[0]!.rule).toBe('Bash(git push:*)')
})

// Claude Code checks the call's permission again inside the call (the
// kit's $.tool.call does since 2.1.309): after the test's own check (the
// verdict the observer sees), `w.verdict` is the person's answer to the ask.
// An ask no one answers comes back as Claude Code's own refusal.
const YES = { decision: 'allow' } as const
const NO = { decision: 'deny' } as const

test('after an ask, the gate spools what came of the call (refused, an error, a result)', async ($, on) => {
  const w = world(on)
  await start($)
  w.verdict = { decision: 'ask' }
  await $.tool.check({ tool: 'Bash', input: { command: 'rm -rf build' }, tool_use_id: 'toolu_4' } as never)
  w.verdict = YES
  w.answer = { deny: 'The user declined' }
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_4', command: 'rm -rf build', description: 'x' } as never)
  w.verdict = { decision: 'ask' }
  await $.tool.check({ tool: 'Bash', input: { command: 'make' }, tool_use_id: 'toolu_5' } as never)
  w.verdict = YES
  w.answer = { result: 'built' }
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_5', command: 'make', description: 'x' } as never)
  w.answer = { result: 'plain' }
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_6', command: 'ls', description: 'x' } as never)
  w.verdict = { decision: 'ask' }
  await $.tool.check({ tool: 'Bash', input: { command: 'rm x' }, tool_use_id: 'toolu_12' } as never)
  w.verdict = NO
  w.answer = { result: 'never reached' }
  const r = await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_12', command: 'rm x', description: 'x' } as never)
  await settle(w, () => spooled(w, 'mod.tool.answer').length >= 3)
  await settle(w, () => false)
  const a = spooled(w, 'mod.tool.answer')
  expect(a.map(x => [x.tool_use_id, x.answered])).toEqual([['toolu_4', 'deny'], ['toolu_5', 'result'], ['toolu_12', 'error']])
  // the person's No: an error in Claude Code's own refusal words, so it never ran
  expect((r as { isError?: boolean }).isError).toBe(true)
  expect(a[2]!.rejected).toBe(true)
})

test('an error result in Claude Code\'s own refusal words is marked rejected (it never ran); another error is not', async ($, on) => {
  const w = world(on)
  await start($)
  w.verdict = { decision: 'ask' }
  await $.tool.check({ tool: 'Bash', input: { command: 'touch x' }, tool_use_id: 'toolu_9' } as never)
  w.verdict = YES // the answer beneath stands in for Claude Code's own words
  w.answer = { result: 'x', isError: true, text: "The user doesn't want to proceed with this tool use. The tool use was rejected." }
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_9', command: 'touch x', description: 'x' } as never)
  w.verdict = { decision: 'deny', rule: 'Bash(touch y)' }
  await $.tool.check({ tool: 'Bash', input: { command: 'touch y' }, tool_use_id: 'toolu_10' } as never)
  w.verdict = YES
  w.answer = { result: 'x', isError: true, text: 'Permission to use Bash with command touch y has been denied.' }
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_10', command: 'touch y', description: 'x' } as never)
  w.verdict = { decision: 'ask' }
  await $.tool.check({ tool: 'Bash', input: { command: 'make' }, tool_use_id: 'toolu_11' } as never)
  w.verdict = YES
  w.answer = { result: 'x', isError: true, text: 'Exit code 2' }
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_11', command: 'make', description: 'x' } as never)
  await settle(w, () => spooled(w, 'mod.tool.answer').length >= 3)
  const a = spooled(w, 'mod.tool.answer')
  expect(a.map(x => [x.tool_use_id, x.answered, x.rejected])).toEqual([
    ['toolu_9', 'error', true], ['toolu_10', 'error', true], ['toolu_11', 'error', false]])
  expect(JSON.stringify(a)).not.toContain('has been denied') // the text never goes to the spool
})

test('the observer never changes a verdict, also when its spool fails', async ($, on) => {
  const w = world(on)
  await start($)
  w.spoolFails = true
  w.verdict = { decision: 'deny', reason: 'gateway: sudo is off on this machine (gateway.toml); ask the user', hook: 'PreToolUse' }
  const v = await $.tool.check({ tool: 'Bash', input: { command: 'sudo ls' }, tool_use_id: 'toolu_7' } as never)
  expect(v.decision).toBe('deny')
  w.verdict = { decision: 'allow', rule: 'Read' }
  const v2 = await $.tool.check({ tool: 'Read', input: { file_path: '/a' }, tool_use_id: 'toolu_8' } as never)
  expect(v2.decision).toBe('allow')
})
