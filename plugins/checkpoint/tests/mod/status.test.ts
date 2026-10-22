// checkpoint's mod under `claude plugin test`: what it shows about itself
// (0.7.3) and where the pause looks for the gateway. The normal states are a
// dim label in the prompt footer (SessionMode), a pause or a failure the
// status line (Claude Code draws every plugin status line with a warning
// sign); the first draw is truthful before SessionStart recorded the env;
// the gateway's contract file is found in the password database's home, as
// Python finds it, not $HOME's.
import { expect, test } from 'claude-code/testing'
import { COMPOSER, FLAG, footer, HOME, PRESENT, ran, settle, SID, start, USER_HOME, world } from './world.ts'
import { pluginDataDir, statusOf } from '../../hooks/mod/logic.ts'

const ENVD = '/data/envs/abc123def456'
const SETTING_UP = 'check: setting up its runtime (first run); pausing works'
const contract = `${USER_HOME}/.config/gateway/run/contract.json`

test('statusOf: normal states for the footer, a pause, failures and unknown for the status line', async () => {
  const s = (paused: boolean, runtime: string, recordingFailed = false) => statusOf({ paused, runtime, recordingFailed })
  expect(s(false, 'ready')).toEqual({ text: 'recording', warn: false })
  expect(s(false, 'older')).toEqual({ text: 'recording · updating its runtime in the background', warn: false })
  expect(s(false, 'building')).toEqual({ text: 'setting up its runtime (first run); pausing works', warn: false })
  expect(s(false, 'missing')).toEqual({ text: 'setting up its runtime (first run); pausing works', warn: false })
  expect(s(false, 'failed').warn).toBe(true)
  expect(s(false, 'failed').text).toContain('FAILED')
  expect(s(false, 'nopixi')).toEqual({ text: 'no runtime: pixi not found · /check-doctor; pausing works', warn: true })
  expect(s(false, '')).toEqual({ text: 'state unknown (/check-doctor)', warn: true })
  expect(s(false, 'ready', true)).toEqual({ text: 'recording FAILED · /check-doctor', warn: true })
  expect(s(true, '')).toEqual({ text: 'paused · type a message or /check:resume to resume', warn: true })
})

test("pluginDataDir: an installed copy's data folder, --plugin-dir's, none without a config folder", async () => {
  expect(pluginDataDir('/c/plugins/cache/checkpoint/check/0.7.3', 'check', '/other')).toBe('/c/plugins/data/check-checkpoint')
  expect(pluginDataDir('/c fg/plugins/cache/my market/check/0.7.3/', 'check', '/other')).toBe('/c fg/plugins/data/check-my-market')
  expect(pluginDataDir('/src/checkpoint/plugins/checkpoint', 'check', '/home/u/.claude')).toBe('/home/u/.claude/plugins/data/check-inline')
  expect(pluginDataDir('/src/checkpoint/plugins/checkpoint', 'check', '/home/u/.claude/')).toBe('/home/u/.claude/plugins/data/check-inline')
  expect(pluginDataDir('/src/checkpoint/plugins/checkpoint', 'check', '/.claude')).toBe('/.claude/plugins/data/check-inline')
  expect(pluginDataDir('/src/checkpoint/plugins/checkpoint', 'check', 'rel/.claude')).toBe('')
})

test('first run, before SessionStart recorded the env: the first draw says setting up (env.sh state), then recording once built', async ($, on) => {
  const w = world(on, { envReady: false })
  w.shell = 'building'
  await start($)
  await settle(w, () => w.stateEnvs.length > 0)
  // The shell looked in this copy's data folder (--plugin-dir here: <config>/plugins/data/check-inline).
  expect(w.stateEnvs[0]).toEqual({ CHECKPOINT_PLUGIN_DATA: '/home/u/.claude/plugins/data/check-inline' })
  expect(await footer($, w)).toEqual(['focus', SETTING_UP])
  // Never "recording" while it builds, and no warning-sign line for a normal state.
  expect(w.statuses).toEqual([])
  // SessionStart records the env; the soon re-check reads it (still building).
  w.files.set(`${HOME}/rt/${SID}`, ENVD)
  await w.clock.advance(300)
  await settle(w, () => w.stateEnvs.length > 1)
  expect(w.stateEnvs.at(-1)).toEqual({ CHECKPOINT_ENVD: ENVD })
  expect(await footer($, w)).toEqual(['focus', SETTING_UP])
  // Built: recording, without the shell (the env's .ready).
  w.files.set(`${ENVD}/.ready`, '')
  const asked = w.stateEnvs.length
  await w.clock.advance(2000)
  await settle(w, () => false)
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
  expect(w.stateEnvs.length).toBe(asked)
  expect(w.statuses).toEqual([])
})

test('CLAUDE_CONFIG_DIR names the --plugin-dir data folder before the env is recorded', async ($, on) => {
  const w = world(on, { envReady: false })
  w.env.CLAUDE_CONFIG_DIR = '/cfg dir'
  w.shell = 'missing'
  await start($)
  await settle(w, () => w.stateEnvs.length > 0)
  expect(w.stateEnvs[0]).toEqual({ CHECKPOINT_PLUGIN_DATA: '/cfg dir/plugins/data/check-inline' })
  expect(await footer($, w)).toEqual(['focus', SETTING_UP])
})

test('warm start, before SessionStart recorded the env: recording from the first draw, never setting up', async ($, on) => {
  const w = world(on, { envReady: false })
  w.shell = 'ready'
  await start($)
  await settle(w, () => w.stateEnvs.length > 0)
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
  expect(w.statuses).toEqual([])
})

for (const [shell, line] of [
  ['failed', 'runtime setup FAILED · /check-doctor; pausing works'],
  ['nopixi', 'no runtime: pixi not found · /check-doctor; pausing works'],
  [undefined, 'state unknown (/check-doctor)'],
] as const) {
  test(`env.sh state ${shell ?? 'failing'} before the env is recorded: the status line (warning), no footer label`, async ($, on) => {
    const w = world(on, { envReady: false })
    w.shell = shell
    await start($)
    await settle(w, () => w.statuses.length > 0)
    expect(w.statuses.at(-1)).toBe(line)
    expect(await footer($, w)).toEqual(['focus'])
  })
}

test('the env recorded but not built, and the shell unable to say: setting up (what the files say)', async ($, on) => {
  const w = world(on, { envReady: false })
  w.files.set(`${HOME}/rt/${SID}`, ENVD)
  await start($)
  await settle(w, () => w.stateEnvs.length > 0)
  expect(await footer($, w)).toEqual(['focus', SETTING_UP])
  expect(w.statuses).toEqual([])
})

test('a pause moves the state to the status line, and a resume back to the footer', async ($, on) => {
  const w = world(on)
  await start($)
  await settle(w, () => false)
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
  expect(w.statuses).toEqual([])
  await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  await settle(w, () => w.statuses.length > 0)
  expect(w.statuses.at(-1)).toBe('paused · type a message or /check:resume to resume')
  expect(await footer($, w)).toEqual(['focus'])
  w.files.delete(FLAG) // the classic hook resumed
  await w.clock.advance(2000)
  await settle(w, () => w.statuses.length > 1)
  expect(w.statuses.at(-1)).toBe(undefined) // the line is cleared
  expect(await footer($, w)).toEqual(['focus', 'check: recording'])
})

// The pause and the gateway: the contract file is where Python looks.
function gwWorld(on: Parameters<typeof world>[0]) {
  const told: string[][] = []
  const w = world(on, {
    answers: {
      'gw-pause': rest => {
        told.push(rest)
        return ran(0, JSON.stringify({ text: 'Gateway jobs of this task: train (train-1, j_X), on_pause interrupt: interrupted.' }) + '\n')
      },
    },
  })
  w.files.set(`${HOME}/jobs.json`, JSON.stringify({ jobs: { 'train-1': { id: 'train-1', name: 'train', state: 'running', session_id: SID, owner: 'gateway' } } }))
  return { w, told }
}

test("the pause tells the gateway when its contract file is in the password database's home ($HOME elsewhere)", async ($, on) => {
  const { w, told } = gwWorld(on)
  w.files.set(contract, '{}') // the gateway's daemon writes it there; nothing under $HOME (/home/u)
  await start($)
  const out = await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(told).toEqual([['gw-pause', SID]])
  expect(out.text).toContain('Gateway jobs of this task: train (train-1, j_X), on_pause interrupt: interrupted.')
  expect(out.text).not.toContain('not told')
  expect(w.runs.filter(r => r[2] === 'contract-path')).toHaveLength(1)
})

test('no contract file in that home: the gateway is not asked, and its jobs are named as not told', async ($, on) => {
  const { w, told } = gwWorld(on)
  w.files.set('/home/u/.config/gateway/run/contract.json', '{}') // $HOME's: not where the gateway writes it
  await start($)
  const out = await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(told).toEqual([])
  expect(out.text).toContain("Gateway jobs, not told of the pause (their pause policies didn't apply): train (train-1)")
})

test('CHECKPOINT_GATEWAY_CONTRACT wins (env.sh not asked); set but empty turns the gateway off', async ($, on) => {
  const a = gwWorld(on)
  a.w.env.CHECKPOINT_GATEWAY_CONTRACT = '/gw/contract.json'
  a.w.files.set('/gw/contract.json', '{}')
  await start($)
  await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(a.told).toEqual([['gw-pause', SID]])
  expect(a.w.runs.filter(r => r[2] === 'contract-path')).toEqual([])
})

test("CHECKPOINT_GATEWAY_CONTRACT set but empty: the gateway's half is off", async ($, on) => {
  const b = gwWorld(on)
  b.w.env.CHECKPOINT_GATEWAY_CONTRACT = ''
  b.w.files.set(contract, '{}')
  await start($)
  await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(b.told).toEqual([])
})

test("env.sh unable to say where the contract file is: Python's gw-pause decides", async ($, on) => {
  const { w, told } = gwWorld(on)
  w.contractPath = undefined
  await start($)
  const out = await $.command.run({ command: 'check-pause', args: '', origin: COMPOSER, presentation: PRESENT })
  expect(told).toEqual([['gw-pause', SID]])
  expect(out.text).toContain('Gateway jobs of this task: train')
})
