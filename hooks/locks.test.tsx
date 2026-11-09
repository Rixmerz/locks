import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import { endedToast, statusLine } from './locks-cli'

type Run = { exitCode: number; stdout: string; stderr?: string }
type Answer = Run | ((argv: readonly string[]) => Run)

/** The world beneath the mod: a session, a fake `locks`, and what the engine was asked. */
function world(on: On, answers: Record<string, Answer>, broken = false) {
  const seen = {
    argv: [] as string[][],
    env: [] as Record<string, string>[],
    tools: [] as { name: string; inputSchema?: Record<string, unknown> }[],
    commands: [] as string[],
    status: [] as (string | undefined)[],
    toasts: [] as string[],
    prompts: [] as string[],
    panes: [] as string[],
  }
  on('session.start', ($, e) => ({ cwd: e.cwd, startedAt: 0, rateLimits: [] }) as never)
  on('turn.start', ($, e) => ({ text: e.text, turnId: e.turnId, kind: 'engine', ref: 0 }) as never)
  on('turn.complete', ($, e) => ({ text: e.answer }) as never)
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/work/shop' }))
  on('session.version', () => ({ value: { version: '2.1.302', base: '2.1.302' } }))
  on('process.run', ($, e) => {
    if (broken) throw new Error('spawn ENOENT')
    const args = e.argv.slice(1)
    seen.argv.push(args)
    seen.env.push({ ...(e.init?.env ?? {}) })
    const key = Object.keys(answers).find(k => args.join(' ').startsWith(k))
    const found = key === undefined ? { exitCode: 0, stdout: '' } : answers[key]
    const ran = typeof found === 'function' ? found(args) : found!
    return { value: { exitCode: ran.exitCode, stdout: ran.stdout, stderr: ran.stderr ?? '', isStdoutTruncated: false,
      isStderrTruncated: false } }
  })
  on('tool.register', ($, e) => {
    seen.tools.push(e)
    return { value: { tool: `mcp__locks__${e.name}` } }
  })
  on('command.register', ($, e) => {
    seen.commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.status', ($, e) => {
    seen.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.open', ($, e) => {
    seen.panes.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('prompt.submit', ($, e) => {
    seen.prompts.push(e.text)
    return { text: e.text }
  })
  return seen
}

const EMPTY_BOARD = JSON.stringify({ counts: { needs_you: 0, running: 0, queued: 0 }, needs_you: [], running: [], queued: [] })

/** Every list in a schema, at any depth: none may be there, a value outside one is refused by the
 * engine as "Invalid tool parameters" before the mod can say what locks takes. */
function enums(schema: unknown): string[] {
  if (!schema || typeof schema !== 'object') return []
  return Object.entries(schema as Record<string, unknown>)
    .flatMap(([key, value]) => (key === 'enum' ? [JSON.stringify(value)] : enums(value)))
}

test('the tools are registered with the project topics named, and no list the engine would refuse by', async ($, on) => {
  mock.clock(on)
  const seen = world(on, {
    'notes --repo /work/shop topics --json': { exitCode: 0, stdout: JSON.stringify([{ name: 'business' }, { name: 'billing' }]) },
    'board': { exitCode: 0, stdout: EMPTY_BOARD },
  })
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  expect(seen.tools.map(t => t.name)).toEqual(['recall', 'note', 'queue', 'tasks', 'resume', 'overview', 'compact'])
  const note = seen.tools.find(t => t.name === 'note')!
  expect((note.inputSchema as any).properties.topic.description).toContain('business, billing;')
  expect((note.inputSchema as any).required).toEqual(['fact'])
  expect(seen.tools.flatMap(t => enums(t.inputSchema))).toEqual([])
  expect(seen.commands).toEqual(['locks'])
})

async function started($: any, on: On, answers: Record<string, Answer>, options?: { wake?: boolean }) {
  const clock = mock.clock(on)
  const seen = world(on, { board: { exitCode: 0, stdout: EMPTY_BOARD }, ...answers })
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  return { seen, clock }
}

function call($: any, tool: string, args: Record<string, unknown>) {
  return $.tool.call({ tool: `mcp__locks__${tool}`, ...args })
}

// --- tools ---------------------------------------------------------------------------

test('without the project topics the five default ones are named', async ($, on) => {
  const { seen } = await started($, on, { 'notes --repo /work/shop topics': { exitCode: 1, stdout: '', stderr: 'boom' } })
  const note = seen.tools.find(t => t.name === 'note')!
  expect((note.inputSchema as any).properties.topic.description)
    .toContain('business, code, decisions, conventions, environment')
})

test('a topics answer that is not JSON falls back too', async ($, on) => {
  const { seen } = await started($, on, { 'notes --repo /work/shop topics': { exitCode: 0, stdout: 'not json' } })
  expect((seen.tools.find(t => t.name === 'recall')!.inputSchema as any).properties.topic.description)
    .toBe('only this topic: business, code, decisions, conventions, environment')
})

test('note passes the fact, the session and only well-formed links, and says which it left out', async ($, on) => {
  const { seen } = await started($, on, {
    note: { exitCode: 0, stdout: JSON.stringify({ id: 7, new: true, topic: 'business', title: 'Cents' }) },
  })
  const answer = await call($, 'note', {
    fact: 'Prices are stored in cents', topic: 'business', anchors: ['cart.py', '', 3],
    links: [{ to: 2, kind: 'explains' }, { to: '#5', kind: 'replaces' }, { to: 'x', kind: 'explains' },
      { to: 3, kind: 'loves' }, null, { to: 0, kind: 'explains' }],
  })
  expect(answer.result).toStartWith('kept #7 [business] Cents. Links left out')
  expect(answer.result).toContain('{"to":3,"kind":"loves"}')
  expect(answer.result).toContain('null')
  expect(answer.result).toContain('depends_on|explains|replaces|contradicts|example_of')
  const i = seen.argv.findIndex(a => a[0] === 'note')
  expect(seen.argv[i]).toEqual(['note', 'Prices are stored in cents', '--repo', '/work/shop', '--json',
    '--topic', 'business', '--anchor', 'cart.py', '--link', '2:explains', '--link', '5:replaces'])
  expect(seen.env[i]).toEqual({ LOCKS_SESSION_ID: 'sess-1' })
})

test('a note without a topic is filed by its words; any topic, an alias included, goes to locks', async ($, on) => {
  const { seen } = await started($, on, {
    note: { exitCode: 0, stdout: JSON.stringify({ id: 8, new: true, topic: 'decisions', title: 'Why' }) },
  })
  expect((await call($, 'note', { fact: 'We chose SQLite because it ships with Python' })).result)
    .toBe('kept #8 [decisions] Why')
  expect(seen.argv.filter(a => a[0] === 'note')[0]).not.toContain('--topic')
  await call($, 'note', { fact: 'Discounts never stack', topic: 'negocio' })
  expect(seen.argv.filter(a => a[0] === 'note')[1]).toContain('negocio')
})

test("a topic locks does not know comes back with the project's topics", async ($, on) => {
  await started($, on, {
    note: { exitCode: 2, stdout: '', stderr: "locks: unknown topic 'misc'; known: business, code" },
  })
  const answer = await call($, 'note', { fact: 'Discounts never stack', topic: 'misc' })
  expect(answer.deny).toBe("locks: unknown topic 'misc'; known: business, code")
})

test('an empty fact is refused without running locks', async ($, on) => {
  const { seen } = await started($, on, {})
  const answer = await call($, 'note', { fact: '   ', topic: 'code' })
  expect(answer.deny).toBe('a note needs a fact')
  expect(seen.argv.some(a => a[0] === 'note')).toBe(false)
})

test("locks' own refusal reaches the model, a secret included", async ($, on) => {
  await started($, on, {
    note: { exitCode: 2, stdout: '', stderr: 'locks: a note needs a fact of a few words, and never a secret' },
  })
  const answer = await call($, 'note', { fact: 'the key is sk-abcdefghijklmnop', topic: 'code' })
  expect(answer.deny).toContain('never a secret')
})

test('a note answer that is not JSON is a refusal, not a silent pass', async ($, on) => {
  await started($, on, { note: { exitCode: 0, stdout: 'kept #1 garbled' } })
  const answer = await call($, 'note', { fact: 'A fact long enough', topic: 'code' })
  expect(answer.deny).toBe('locks note failed')
})

test('recall builds its question, topic and path, and says when nothing matches', async ($, on) => {
  const { seen } = await started($, on, { recall: { exitCode: 0, stdout: '' } })
  const answer = await call($, 'recall', { question: 'discounts', topic: 'business', path: 'cart.py' })
  expect(answer.result).toBe('no notes match')
  expect(seen.argv.find(a => a[0] === 'recall')).toEqual(['recall', 'discounts', '--repo', '/work/shop', '--topic',
    'business', '--path', 'cart.py'])
  const bare = await call($, 'recall', {})
  expect(bare.result).toBe('no notes match')
})

test('a recall that fails is a refusal with the reason', async ($, on) => {
  await started($, on, { recall: { exitCode: 2, stdout: '', stderr: "locks: unknown topic 'x'" } })
  expect((await call($, 'recall', { question: 'q', topic: 'x' })).deny).toContain('unknown topic')
})

test('queue refuses a task too short to be a brief, and runs nothing', async ($, on) => {
  const { seen } = await started($, on, {})
  for (const task of ['', 'fix', '  do it ']) {
    expect((await call($, 'queue', { task })).deny).toContain('write the task in full')
  }
  expect(seen.argv.some(a => a[0] === 'queue')).toBe(false)
})

test('queue passes the check, a known kind only, and the session', async ($, on) => {
  const { seen } = await started($, on, {
    'queue add': { exitCode: 0, stdout: JSON.stringify({ id: 4, title: 'Add the discount', then: 'A worker takes it now.' }) },
  })
  const answer = await call($, 'queue', { task: 'Add the volume discount to cart.total', verify: 'npm test', kind: 'hack' })
  expect(answer.result).toContain('queued #4 — Add the discount. A worker takes it now.')
  // a kind it does not have queues the task anyway, and the model hears why it was dropped
  expect(answer.result).toContain('The kind "hack" is not one of locks\'')
  const i = seen.argv.findIndex(a => a[0] === 'queue')
  expect(seen.argv[i]).toEqual(['queue', 'add', 'Add the volume discount to cart.total', '--repo', '/work/shop',
    '--json', '--verify', 'npm test'])
  expect(seen.env[i]).toEqual({ LOCKS_SESSION_ID: 'sess-1' })
  const known = await call($, 'queue', { task: 'Reproduce the double charge on retry', kind: 'debug-repro', verify: 7 })
  expect(known.result).not.toContain('not one of')
  expect(seen.argv.filter(a => a[0] === 'queue')[1]).toEqual(['queue', 'add', 'Reproduce the double charge on retry',
    '--repo', '/work/shop', '--json', '--kind', 'debug-repro'])
})

test('queue says each piece of a request locks split before queueing it', async ($, on) => {
  await started($, on, {
    'queue add': { exitCode: 0, stdout: JSON.stringify({ id: 7, title: 'Add the receipt', then: 'Workers take them now.',
      split: true, tasks: [{ id: 7, title: 'Add the receipt' }, { id: 8, title: 'Add the CSV export' }] }) },
  })
  const answer = await call($, 'queue', { task: 'Add a receipt and a CSV export of an order, with tests' })
  expect(answer.result).toContain('split into 2 tasks and queued: #7 — Add the receipt; #8 — Add the CSV export.')
  expect(answer.result).toContain('Their endings reach this session')
})

test('resume never carries a rule or an approval the model asks for', async ($, on) => {
  const { seen } = await started($, on, { resume: { exitCode: 0, stdout: 'resuming #12 in the background' } })
  const answer = await call($, 'resume', { id: 12, allow: ['Bash(*)'], allow_approval: true, max_turns: 'lots' })
  expect(answer.result).toContain('resuming #12')
  expect(seen.argv.find(a => a[0] === 'resume')).toEqual(['resume', '12', '--unattended', '--detach'])
})

test('resume takes an id and turns written as digits, as a model often sends them', async ($, on) => {
  const { seen } = await started($, on, { resume: { exitCode: 0, stdout: 'resuming in the background' } })
  await call($, 'resume', { id: '42', max_turns: '120' })
  await call($, 'resume', { id: ' #42 ', max_turns: '-5' })
  expect(seen.argv.filter(a => a[0] === 'resume')).toEqual([
    ['resume', '42', '--unattended', '--detach', '--max-turns', '120'],
    ['resume', '42', '--unattended', '--detach'],
  ])
})

test('resume: what only the person can clear comes back as a refusal', async ($, on) => {
  await started($, on, { resume: { exitCode: 3, stdout: '', stderr: 'task #12 was refused npm test: only the person can allow Bash(npm test:*)' } })
  expect((await call($, 'resume', { id: 12, max_turns: 150 })).deny).toContain('only the person can allow')
})

test('resume refuses an id that is no id, before running anything', async ($, on) => {
  const { seen } = await started($, on, {})
  for (const id of ['abc', -1, 0, 2.5, undefined, '1e3', '12abc', '', null, [12], 2 ** 60]) {
    expect((await call($, 'resume', { id })).deny).toContain('resume needs a task id')
  }
  expect(seen.argv.some(a => a[0] === 'resume')).toBe(false)
})

test('the reading tools are not put to the person; queue, resume and a deny are left as they are', async ($, on) => {
  let base: 'ask' | 'deny' | 'allow' = 'ask'
  on('tool.check', () => ({ decision: base, reason: 'the engine' }))
  const { seen } = await started($, on, { 'config delegate': { exitCode: 0, stdout: 'false' } })
  const verdict = (name: string) => $.tool.check({ tool: `mcp__locks__${name}`, input: {} })
  for (const name of ['recall', 'tasks', 'note', 'overview']) {
    expect((await verdict(name)).decision).toBe('allow')
  }
  // with `delegate off`, queue and resume spend money on the person's own call
  expect((await verdict('queue')).decision).toBe('ask')
  expect((await verdict('resume')).decision).toBe('ask')
  expect(seen.argv.filter(a => a[0] === 'config')).toEqual([['config', 'delegate'], ['config', 'delegate']])
  // another tool, even one named like ours, is never touched
  expect((await $.tool.check({ tool: 'mcp__other__recall', input: {} })).decision).toBe('ask')
  expect((await $.tool.check({ tool: 'Bash', input: { command: 'locks recall x' } })).decision).toBe('ask')
  // a deny stays a deny
  base = 'deny'
  for (const name of ['recall', 'tasks', 'note', 'overview']) {
    expect((await verdict(name)).decision).toBe('deny')
  }
})

test('with delegate on, an ask for queue and resume is the orchestrator\'s, and a deny stays a deny', async ($, on) => {
  let base: 'ask' | 'deny' | 'allow' = 'ask'
  on('tool.check', () => ({ decision: base, reason: 'the engine' }))
  const { seen } = await started($, on, {
    'config delegate': { exitCode: 0, stdout: 'true' },
    resume: { exitCode: 0, stdout: 'resuming #12 in the background' },
  })
  const verdict = (name: string) => $.tool.check({ tool: `mcp__locks__${name}`, input: {} })
  for (const name of ['queue', 'resume', 'overview']) {
    expect((await verdict(name)).decision).toBe('allow')
  }
  expect((await verdict('queue')).reason).toContain('the person delegated')
  // an organization's ceiling or a rule that denies still decides
  base = 'deny'
  for (const name of ['queue', 'resume']) expect((await verdict(name)).decision).toBe('deny')
  // a resume it lets through still runs unattended, with no rule or approval the model asked for
  await call($, 'resume', { id: 12, allow: ['Bash(*)'], allow_approval: true, keep: true })
  expect(seen.argv.find(a => a[0] === 'resume')).toEqual(['resume', '12', '--unattended', '--detach'])
})

test('a delegate locks cannot read leaves the ask with the person', async ($, on) => {
  on('tool.check', () => ({ decision: 'ask', reason: 'the engine' }))
  await started($, on, { 'config delegate': { exitCode: 1, stdout: '', stderr: 'boom' } })
  for (const name of ['queue', 'resume']) {
    expect((await $.tool.check({ tool: `mcp__locks__${name}`, input: {} })).decision).toBe('ask')
  }
})

test('a locks that cannot run is said, never a hang', async ($, on) => {
  mock.clock(on)
  const seen = world(on, {}, true)
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  expect(seen.tools.length).toBe(7)
  expect((await call($, 'tasks', {})).deny).toBe('locks could not read its board')
  expect((await call($, 'recall', { question: 'x' })).deny).toContain('locks did not run')
})

const BOARD = {
  counts: { needs_you: 2, orchestrator: 1, running: 1, queued: 1 },
  needs_you: [
    { id: 12, title: 'make npm test pass', status: 'blocked', stop: { cause: 'permission', who: 'your permission settings',
      reason: 'the worker was refused Bash(npm test)', todo: 'allow it', next: "locks resume 12 --allow 'Bash(npm test:*)'",
      allow: ['Bash(npm test:*)'] } },
    { id: 13, title: 'reduce the app', status: 'replan', waits_on: 'orchestrator', stop: { cause: 'spec', who: "the worker's verdict",
      reason: 'the API has no such field', todo: 'rewrite it', next: null, allow: [] } },
    { id: 9, title: 'docs', status: 'done', branch: 'locks/task-9', asks: 'review branch locks/task-9, then merge it' },
  ],
  running: [{ id: 14, title: 'write stats', status: 'running', current_cell: 'sonnet/medium',
    progress_line: 'implementing · 4m of ≈7m · 3 files +120 −8', files_line: 'wrote stats.py, test_stats.py',
    slow: 'running 101m: longer than 9 in 10 of the 6 ui tasks that passed here (p90 40m)' }],
  queued: [{ repo: 'r', paused: false, reason: null, tasks: [{ id: 15, title: 'add median', status: 'queued' }] }],
}

test('tasks says what waits on the person and how it goes on', async ($, on) => {
  await started($, on, { board: { exitCode: 0, stdout: JSON.stringify(BOARD) } })
  const answer = await call($, 'tasks', {})
  expect(answer.result).toContain('ask the person in this conversation (only they can grant it), then run its line: #12 [blocked] make npm test pass')
  expect(answer.result).toContain('yours to decide (resume, rewrite or dismiss with why): #13 [replan] reduce the app')
  expect(answer.result).toContain("allow it: locks resume 12 --allow 'Bash(npm test:*)'")
  expect(answer.result).toContain('review branch locks/task-9, then merge it')
  expect(answer.result).toContain('running: #14 write stats (sonnet/medium)\n  now: implementing · 4m of ≈7m · 3 files +120 −8\n  wrote stats.py, test_stats.py\n  slow: running 101m')
  expect(answer.result).toContain('queued: #15 add median')
})

test('overview reads every session through locks, 24 hours unless asked', async ($, on) => {
  const text = 'locks: sessions seen since 2026-10-09T00:00:00+00:00, and their work'
  const { seen } = await started($, on, {
    'overview --hours 24': { exitCode: 0, stdout: text },
    'overview --hours 6': { exitCode: 0, stdout: text + ' (6h)' },
  })
  expect((await call($, 'overview', {})).result).toBe(text)
  expect((await call($, 'overview', { hours: '6' })).result).toBe(text + ' (6h)')
  expect((await call($, 'overview', { hours: -3 })).result).toBe(text)
  expect(seen.argv.filter(a => a[0] === 'overview').length).toBe(3)
})

// --- waking the session -------------------------------------------------------------------

const NOTICE = 'locks: work this session sent has ended since you last heard.\n#21 [done] add median'

test('what ended while the session was idle starts a turn, once', async ($, on) => {
  let calls = 0
  const { seen, clock } = await started($, on, {
    endings: () => ({ exitCode: 0, stdout: JSON.stringify(calls++ === 0 ? { notice: NOTICE, ids: [21] } : { notice: null, ids: [] }) }),
  })
  await clock.advance(15_000)
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([NOTICE])
  expect(seen.toasts).toEqual(['locks: #21 ended'])  // an older locks says no status: the words alone
  expect(seen.argv.filter(a => a[0] === 'endings')[0]).toEqual(['endings', '--session', 'sess-1', '--json'])
})

test('a task that ended touches the pulse, and the idle session hears of it within seconds', async ($, on) => {
  let mtime = 1000
  let ended = false
  on('fs.stat', ($, e) => (e.path === '/home/.locks/pulse'
    ? { value: { kind: 'file', size: 0, mtimeMs: mtime, isLink: false } }
    : { error: 'ENOENT' }) as never)
  const { seen, clock } = await started($, on, {
    endings: () => ({ exitCode: 0, stdout: JSON.stringify(ended
      ? { notice: NOTICE, ids: [21], pulse: '/home/.locks/pulse' }
      : { notice: null, ids: [], pulse: '/home/.locks/pulse' }) }),
  })
  await clock.advance(15_000)  // the first poll names the pulse
  await clock.advance(2_000)  // the first look only learns where it stands
  expect(seen.prompts).toEqual([])
  ended = true
  mtime = 2000
  await clock.advance(2_000)
  expect(seen.prompts).toEqual([NOTICE])
  const asked = seen.argv.filter(a => a[0] === 'endings').length
  await clock.advance(10_000)  // an unchanged pulse runs nothing
  expect(seen.argv.filter(a => a[0] === 'endings').length).toBe(asked)
})

test('the toast says how each task ended, in the board\'s colors', async ($, on) => {
  const { seen, clock } = await started($, on, {
    endings: { exitCode: 0, stdout: JSON.stringify({ notice: NOTICE, ids: [21, 22, 23],
      statuses: { 21: 'done', 22: 'blocked', 23: 'mystery' } }) },
  })
  await clock.advance(15_000)
  expect(seen.toasts[0]).toBe('locks: #21 ✅ passed · #22 🟠 blocked · #23 ended')
})

test('nothing ended: no turn, no toast', async ($, on) => {
  const { seen, clock } = await started($, on, { endings: { exitCode: 0, stdout: JSON.stringify({ notice: null, ids: [] }) } })
  await clock.advance(45_000)
  expect(seen.prompts).toEqual([])
  expect(seen.toasts).toEqual([])
})

test('while a turn runs, endings are left for the Stop hook', async ($, on) => {
  const { seen, clock } = await started($, on, { endings: { exitCode: 0, stdout: JSON.stringify({ notice: NOTICE, ids: [21] }) } })
  await $.turn.start({ text: 'go', turnId: 't1' } as never)
  await clock.advance(30_000)
  expect(seen.argv.some(a => a[0] === 'endings')).toBe(false)
  expect(seen.prompts).toEqual([])
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as never)
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([NOTICE])
  // a subagent's turn inside the main one does not leave the session marked busy
  // a subagent's turn ending inside a main one leaves the session busy
  await $.turn.start({ text: 'again', turnId: 't2' } as never)
  await $.turn.complete({ answer: 'sub', durationMs: 1, isAborted: false, turnId: 't3', agentId: 'a1', reason: 'answer' } as never)
  const asked = seen.argv.filter(a => a[0] === 'endings').length
  await clock.advance(30_000)
  expect(seen.argv.filter(a => a[0] === 'endings').length).toBe(asked)
})

test('with wake off the session is never woken', { options: { wake: false } }, async ($, on) => {
  const { seen, clock } = await started($, on, { endings: { exitCode: 0, stdout: JSON.stringify({ notice: NOTICE, ids: [21] }) } })
  await clock.advance(60_000)
  expect(seen.argv.some(a => a[0] === 'endings')).toBe(false)
  expect(seen.prompts).toEqual([])
})

test('an endings answer that fails or is garbled wakes nothing', async ($, on) => {
  let n = 0
  const { seen, clock } = await started($, on, {
    endings: () => (n++ === 0 ? { exitCode: 1, stdout: '', stderr: 'locked' } : { exitCode: 0, stdout: '{"notice": ' }),
  })
  await clock.advance(30_000)
  expect(seen.prompts).toEqual([])
})

// --- compaction at locks' moment -----------------------------------------------------------

const LINE = '/compact [locks-compaction #7] locks asked for this compaction at a break between tasks.'
const compactionCalls = (seen: { argv: string[][] }) => seen.argv.filter(a => a[0] === 'compaction')

test('suggest: the line is shown to the person once, never sent to the model', async ($, on) => {
  let calls = 0
  const { seen, clock } = await started($, on, {
    'compaction next': () => ({ exitCode: 0, stdout: JSON.stringify(calls++ === 0
      ? { action: 'suggest', decision: 7, line: LINE } : { action: null }) }),
  })
  await clock.advance(15_000)
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([])
  expect(seen.toasts).toHaveLength(1)
  expect(seen.toasts[0]).toContain('a good moment to compact')
  expect(seen.toasts[0]).toContain(LINE)
  expect(compactionCalls(seen)[0]).toEqual(['compaction', 'next', '--session', 'sess-1', '--version', '2.1.302', '--json'])
})

test('orchestrator: locks\' moment is advice the session gets, once; it decides', async ($, on) => {
  let calls = 0
  const advice = 'locks [compaction #7]: this looks like a good moment to compact this session (…). It is your call.'
  const { seen, clock } = await started($, on, {
    'compaction next': () => ({ exitCode: 0, stdout: JSON.stringify(calls++ === 0
      ? { action: 'advise', decision: 7, advice } : { action: null }) }),
  })
  await clock.advance(15_000)
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([advice])
  expect(seen.toasts).toEqual([])
})

test('compact: the session decides; locks takes it, and it runs at the next idle look', async ($, on) => {
  const asked = compacts(on, () => ({ messages: [{ role: 'user', text: 'the summary', toolUses: [] }] }))
  let n = 0
  const { seen, clock } = await started($, on, {
    'compaction request': () => ({ exitCode: 0, stdout: JSON.stringify(n++ === 0 ? { id: 7 } : { error: 'off' }) }),
    'compaction next': () => ({ exitCode: 0, stdout: SUBMIT }),
    'compaction delivered': { exitCode: 0, stdout: JSON.stringify({ kept: true }) },
  })
  const answer = await call($, 'compact', { why: 'the feature landed', keep: 'the open question on refunds' })
  expect(answer.result).toStartWith('compaction #7 is set: it runs when this turn ends')
  expect(compactionCalls(seen)[0]).toEqual(['compaction', 'request', '--session', 'sess-1', '--cwd', '/work/shop',
    '--why', 'the feature landed', '--json', '--keep', 'the open question on refunds'])
  await clock.advance(15_000)
  expect(asked).toEqual([INSTRUCTIONS])
  expect((await call($, 'compact', { why: 'x' })).deny).toBe('off')
})

test('compact with then: once the compaction stood, the prompt the session left itself is its next turn',
  async ($, on) => {
    const asked = compacts(on, () => ({ messages: [{ role: 'user', text: 'the summary', toolUses: [] }] }))
    const follow = 'locks [compaction #7]: this session compacted. … "merge #12, then start the export". Go on with it now.'
    const steps = [SUBMIT, JSON.stringify({ action: 'continue', decision: 7, text: follow }), JSON.stringify({ action: null })]
    const { seen, clock } = await started($, on, {
      'compaction request': { exitCode: 0, stdout: JSON.stringify({ id: 7 }) },
      'compaction next': () => ({ exitCode: 0, stdout: steps.shift() ?? JSON.stringify({ action: null }) }),
      'compaction delivered': { exitCode: 0, stdout: JSON.stringify({ kept: true }) },
    })
    const answer = await call($, 'compact', { why: 'the feature landed', then: 'merge #12, then start the export' })
    expect(answer.result).toEndWith('your `then` reaches you as your next prompt.')
    expect(compactionCalls(seen)[0]).toEqual(['compaction', 'request', '--session', 'sess-1', '--cwd', '/work/shop',
      '--why', 'the feature landed', '--json', '--then', 'merge #12, then start the export'])
    await clock.advance(15_000)
    expect(asked).toEqual([INSTRUCTIONS])
    expect(seen.prompts).toEqual([])
    // The mod looks again after the compaction, with no turn ended in between.
    await clock.advance(15_000)
    expect(seen.prompts).toEqual([follow])
  })

const INSTRUCTIONS = LINE.slice('/compact '.length)
const SUBMIT = JSON.stringify({ action: 'submit', decision: 7, line: LINE, instructions: INSTRUCTIONS })
const DELIVERED = ['compaction', 'delivered', '7', '--session', 'sess-1', '--version', '2.1.302']

/** The engine's compaction, as the mod calls it: what it was asked with, and its answer. */
function compacts(on: On, answer: () => object) {
  const asked: (string | undefined)[] = []
  on('session.compact', ($, e) => {
    asked.push(e.instructions)
    return answer() as never
  })
  return asked
}

test('auto: the engine compacts between turns with the instructions, said after it stood, and locks is told',
  async ($, on) => {
    let calls = 0
    const asked = compacts(on, () => ({ messages: [{ role: 'user', text: 'the summary', toolUses: [] }] }))
    const { seen, clock } = await started($, on, {
      'compaction next': () => ({ exitCode: 0, stdout: calls++ === 0 ? SUBMIT : JSON.stringify({ action: null }) }),
      'compaction delivered': { exitCode: 0, stdout: JSON.stringify({ kept: true }) },
    })
    await clock.advance(15_000)
    expect(asked).toEqual([INSTRUCTIONS])
    expect(seen.prompts).toEqual([])  // nothing reaches the model as a message
    expect(compactionCalls(seen)[1]).toEqual([...DELIVERED, '--ran', '--json'])
    expect(seen.toasts).toEqual(["locks: compacted (#7) between turns with locks' /compact instructions"])
  })

test('auto: a hook that vetoes it is said, and is no verdict on the build', async ($, on) => {
  compacts(on, () => ({ skip: 'blocked by a PreCompact hook' }))
  const { seen, clock } = await started($, on, {
    'compaction next': { exitCode: 0, stdout: SUBMIT },
    'compaction delivered': { exitCode: 0, stdout: JSON.stringify({ kept: true }) },
  })
  await clock.advance(15_000)
  expect(compactionCalls(seen)[1]).toEqual([...DELIVERED, '--skipped', 'blocked by a PreCompact hook', '--json'])
  expect(seen.toasts).toEqual(['locks: the compaction locks asked for (#7) was skipped: blocked by a PreCompact hook'])
})

/** `/compact` as the mod runs it where the engine has no compaction between turns. */
function runsCompact(on: On, answer: () => object) {
  const asked: string[] = []
  on('command.run', { command: 'compact' }, ($, e) => {
    asked.push(e.args)
    return answer() as never
  })
  return asked
}

test('auto, headless: with no compaction between turns, /compact runs with the instructions and locks settles it',
  async ($, on) => {
    compacts(on, () => { throw new Error('$.session.compact: not available in a headless (-p / SDK) session yet') })
    const ran = runsCompact(on, () => ({ text: 'Compacted' }))
    const { seen, clock } = await started($, on, {
      'compaction next': { exitCode: 0, stdout: SUBMIT },
      'compaction delivered': { exitCode: 0, stdout: JSON.stringify({ kept: true }) },
    })
    await clock.advance(15_000)
    expect(ran).toEqual([INSTRUCTIONS])
    expect(seen.prompts).toEqual([])
    // The submit stays open: the hooks that see its marker settle it.
    expect(compactionCalls(seen)).toHaveLength(1)
    expect(seen.toasts).toEqual(["locks: compacting (#7): ran /compact with locks' instructions"])
  })

test('auto: a call that fails is no refusal of the build: the line is shown, and nothing said before it',
  async ($, on) => {
    // A hook that throws is passed over, and the engine's own compaction, with no
    // transcript here, rejects the call: a failure the mod cannot read the build from.
    compacts(on, () => { throw new Error('a turn is running') })
    runsCompact(on, () => { throw new Error('a turn is running') })
    const { seen, clock } = await started($, on, {
      'compaction next': { exitCode: 0, stdout: SUBMIT },
      'compaction delivered': { exitCode: 0, stdout: JSON.stringify({ kept: true }) },
    })
    await clock.advance(15_000)
    const [, delivered] = compactionCalls(seen)
    expect(delivered!.slice(0, 8)).toEqual([...DELIVERED, '--rejected'])
    expect(delivered![8]).toContain('HooksError')
    expect(delivered).not.toContain('--refused')
    expect(seen.prompts).toEqual([])
    expect(seen.toasts).toHaveLength(1)
    expect(seen.toasts[0]).toContain(LINE)
  })

test('never during a turn: locks is not even asked until it ends', async ($, on) => {
  const asked = compacts(on, () => ({ messages: [{ role: 'user', text: 'the summary', toolUses: [] }] }))
  const { seen, clock } = await started($, on, {
    'compaction next': { exitCode: 0, stdout: SUBMIT },
  })
  await $.turn.start({ text: 'go', turnId: 't1' } as never)
  await clock.advance(30_000)
  expect(compactionCalls(seen)).toEqual([])
  expect(seen.toasts).toEqual([])
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as never)
  await clock.advance(15_000)
  expect(compactionCalls(seen)[0]!.slice(0, 2)).toEqual(['compaction', 'next'])
  expect(asked).toEqual([INSTRUCTIONS])
  expect(seen.toasts[0]).toContain('compacted (#7)')
})

test('idle polls ask locks only after a turn ended, or while it says a decision waits', async ($, on) => {
  const answers = [{ action: null, wait: true }, { action: null }]
  const { seen, clock } = await started($, on, {
    'compaction next': () => ({ exitCode: 0, stdout: JSON.stringify(answers.shift() ?? { action: null }) }),
  })
  await clock.advance(15_000)  // once after a (re)load
  await clock.advance(15_000)  // asked again: a decision waits on a run holding the checkout
  await clock.advance(60_000)  // nothing waits: four idle polls start no process
  expect(compactionCalls(seen)).toHaveLength(2)
  await $.turn.start({ text: 'go', turnId: 't2' } as never)
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't2', reason: 'answer' } as never)
  await clock.advance(30_000)
  expect(compactionCalls(seen)).toHaveLength(3)
})

test('a turn that starts while locks answers gets the line shown, and locks is told it was not submitted',
  async ($, on) => {
    const { seen, clock } = await started($, on, {
      'compaction next': () => {
        void $.turn.start({ text: 'typed meanwhile', turnId: 't9' } as never)
        return { exitCode: 0, stdout: JSON.stringify({ action: 'submit', decision: 7, line: LINE }) }
      },
    })
    await clock.advance(15_000)
    expect(seen.prompts).toEqual([])
    expect(compactionCalls(seen)[1]).toEqual(['compaction', 'delivered', '7', '--session', 'sess-1'])
    expect(seen.toasts[0]).toContain(LINE)
  })

test('a build found not to run it: locks says so, and the line is shown instead', async ($, on) => {
  const said = 'locks: this Claude Code (2.1.302) did not run the /compact locks submitted'
  const { seen, clock } = await started($, on, {
    'compaction next': { exitCode: 0, stdout: JSON.stringify({ action: 'suggest', decision: 8, line: LINE, said }) },
  })
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([])
  expect(seen.toasts[0]).toBe(said)
  expect(seen.toasts[1]).toContain(LINE)
})

test('ending work wakes first; a compaction waits for the next idle look', async ($, on) => {
  let n = 0
  const { seen, clock } = await started($, on, {
    endings: () => ({ exitCode: 0, stdout: JSON.stringify(n++ === 0 ? { notice: NOTICE, ids: [21] } : { notice: null, ids: [] }) }),
    'compaction next': { exitCode: 0, stdout: JSON.stringify({ action: null }) },
  })
  await clock.advance(15_000)
  expect(compactionCalls(seen)).toEqual([])
  await clock.advance(15_000)
  expect(compactionCalls(seen)).toHaveLength(1)
})

test('a compaction answer that fails or is garbled does nothing', async ($, on) => {
  let n = 0
  const { seen, clock } = await started($, on, {
    'compaction next': () => (n++ === 0 ? { exitCode: 1, stdout: '', stderr: 'locked' } : { exitCode: 0, stdout: '{"action": ' }),
  })
  await clock.advance(30_000)
  expect(seen.prompts).toEqual([])
  expect(seen.toasts).toEqual([])
})

// --- the status entry ---------------------------------------------------------------------

test('the status entry counts the board and clears when it is empty', async ($, on) => {
  let board = JSON.stringify(BOARD)
  const { seen, clock } = await started($, on, { board: () => ({ exitCode: 0, stdout: board }) })
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe('locks · 🟣 3 for the orchestrator · 🔵 1 running (#14 implementing · 4m of ≈7m · 3 files +120 −8) · ⚪ 1 queued')
  // a locks that says no progress: the phases, as before
  board = JSON.stringify({ ...BOARD, running: [{ ...BOARD.running[0], phase: 'checking', progress_line: null }] })
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe('locks · 🟣 3 for the orchestrator · 🔵 1 running (checking) · ⚪ 1 queued')
  // the waits this session sent come first, the rest of the project's apart
  board = JSON.stringify({ ...BOARD, counts: { ...BOARD.counts, mine: 1 } })
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe('locks · 🟣 1 for the orchestrator · 2 more in the project · 🔵 1 running (#14 implementing · 4m of ≈7m · 3 files +120 −8) · ⚪ 1 queued')
  expect(seen.argv.some(a => a[0] === 'board' && a.includes('--session') && a.includes('sess-1'))).toBe(true)
  board = EMPTY_BOARD
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe(undefined)
  board = 'not json'
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe(undefined)
})

// --- /locks -------------------------------------------------------------------------------

test('/locks starts the web board detached and says where it is; no pane, no band', async ($, on) => {
  const { seen } = await started($, on, { board: { exitCode: 0, stdout: JSON.stringify(BOARD) } })
  const ran = await $.command.run({ command: 'locks', args: '' } as never)
  expect(ran.text).toBe('locks board: http://127.0.0.1:8790/')
  expect(seen.panes).toHaveLength(0)
  const spawned = seen.argv.find(args => args[0] === '-c')
  expect(spawned?.[1]).toContain('ui --open >/dev/null')
  expect(spawned?.[1]).not.toContain('--tabs')
})

test('the status line tells each of a few running tasks how far it got, and counts more', () => {
  const counts = { needs_you: 0, running: 1, queued: 0 }
  expect(statusLine(counts, [{ id: 7, phase: 'implementing', progress_line: 'implementing · 4m of ≈7m · 2 files +40 −3' }]))
    .toBe('locks · 🔵 1 running (#7 implementing · 4m of ≈7m · 2 files +40 −3)')
  const many = [1, 2, 3].map(id => ({ id, phase: 'exploring', progress_line: 'exploring · 1m' }))
  expect(statusLine({ ...counts, running: 3 }, many)).toContain('3 running (exploring)')
})

test('a notice with nothing ended is a paused lane, and its toast says so', () => {
  expect(endedToast([])).toBe('locks: a lane holding your queued work is paused')
})
