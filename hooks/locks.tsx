// locks as a Claude Code mod: the same core, reached from inside the session.
//
// - Tools the model calls by name (recall, note, queue, tasks, resume, overview) instead
//   of `locks` through Bash: no PATH to find, no permission rule per command.
//   Their schemas are loose on purpose: a value outside a list is read by the
//   handler and answered with what locks takes, never refused as "Invalid tool
//   parameters", which tells the model nothing.
// - Work a session queued that ends while it is idle wakes it: a turn starts
//   with the notice, and the session tells the person and goes on.
// - Compacting is the orchestrating session's call (`compact orchestrator`): at
//   a good moment locks sees at a turn's end, the session is told why and
//   decides; it compacts with the `compact` tool, on that advice or on its own,
//   and the engine compacts with locks' instructions once its turn ends (in a
//   headless session, where the engine has no such call, `/compact` runs with
//   them). A `then` the session gave is submitted as its next prompt once the
//   compaction finished. With `compact suggest` the line is shown to the person;
//   with `auto` the engine compacts at locks' moment. A toast says what ran.
// - A status entry that counts what waits on the orchestrator, runs and is queued,
//   and `/locks`, which opens the web board: the one place to look and press
//   (resume, allow, dismiss, unpause). A terminal copy of it was a poorer one.
//
// The classic hooks keep working without the mod: endings still reach the
// session at the end of a turn or with the next prompt, and the model can
// still run `locks` through Bash. Nothing here grants a permission on the
// model's behalf: a refused command is allowed by the person's own press.
import type { EngineInterface, Register } from 'claude-code'

import type { LocksBoard } from '../types'
import type { CompactionStep, Ran } from './locks-cli'
import { commandToast, compactedToast, DEFAULT_TOPICS, endedToast, json, skippedToast, statusLine, suggestToast } from './locks-cli'

const BOARD_URL = 'http://127.0.0.1:8790/'
const POLL_MS = 15_000
/** How often the mod looks at locks' pulse, a file touched each time a task ends: a stat,
 * not a process, so an ending wakes an idle session within it rather than within POLL_MS. */
const PULSE_MS = 2_000
/** How long a toast with a `/compact` line to paste, or a fallback to say, stays: the longest the engine takes. */
const SUGGEST_MS = 60_000
// How long `queue` waits for `locks queue add`: a request that is several tasks is split first, and a
// split Haiku proposes took more than the default minute.
const QUEUE_MS = 300_000
const KINDS = ['chat', 'explore', 'docs', 'test', 'refactor', 'implement', 'ui', 'feature', 'debug-repro',
  'debug-unclear', 'review-routine', 'review-critical', 'plan']
const LINKS = ['depends_on', 'explains', 'replaces', 'contradicts', 'example_of']


const RECALL = 'mcp__locks__recall'
const NOTE = 'mcp__locks__note'
const QUEUE = 'mcp__locks__queue'
const TASKS = 'mcp__locks__tasks'
const RESUME = 'mcp__locks__resume'
const OVERVIEW = 'mcp__locks__overview'
const COMPACT = 'mcp__locks__compact'
/** The tools that only read the project's notes and board, every session's
 * work, or keep a note in locks' own store: asking the person for each is a
 * prompt that protects nothing. */
const QUIET = [RECALL, TASKS, NOTE, OVERVIEW]
/** The tools that start work and spend money: the person's call, unless they
 * delegated it to the orchestrator (`locks config delegate`, on by default).
 * `resume` runs `--unattended` either way, so what only a person may clear is
 * still refused. */
const DELEGATED = [QUEUE, RESUME, COMPACT]

/** A plugin tool's arguments, as the model gave them: loose until read. */
function input(e: object): Record<string, unknown> {
  return e as Record<string, unknown>
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** A positive whole number, as a number or its digits ("42", "#42"); else null. */
function whole(value: unknown): number | null {
  const n = typeof value === 'number' ? value
    : typeof value === 'string' && /^#?\d+$/.test(value.trim()) ? Number(value.trim().replace('#', ''))
    : NaN
  return Number.isSafeInteger(n) && n > 0 ? n : null
}

// How the mod reaches locks: the plugin's own launcher, run by argv. The core
// stays in Python; this layer only asks it and draws what it answers.

/** Run `locks <args>` from the session's directory. Never rejects. */
async function locks(
  $: EngineInterface,
  args: readonly string[],
  init: { session?: string; timeoutMs?: number } = {},
): Promise<Ran> {
  const bin = `${$.plugin.root}/bin/locks`
  try {
    const ran = await $.process.run([bin, ...args], {
      // The session's id goes with the commands that act for it, so what they
      // queue or keep is that session's, as when it runs `locks` through Bash.
      env: init.session ? { LOCKS_SESSION_ID: init.session } : {},
      timeoutMs: init.timeoutMs ?? 60_000,
    })
    return { ok: ran.exitCode === 0, out: ran.stdout.trim(), err: ran.stderr.trim(), code: ran.exitCode }
  } catch (error) {
    return { ok: false, out: '', err: `locks did not run: ${String(error)}`, code: -1 }
  }
}

/** The board of one project, as `locks board --full --repo` answers it; with
 * `session`, its counts say how many waits are that session's work. */
async function board($: EngineInterface, cwd: string, session?: string): Promise<LocksBoard | null> {
  const full = json<LocksBoard>(await locks($, ['board', '--full', '--repo', cwd,
    ...(session ? ['--session', session] : [])]))
  return full && full.counts ? full : null
}

async function topics($: EngineInterface, cwd: string): Promise<string[]> {
  const found = json<{ name: string }[]>(await locks($, ['notes', '--repo', cwd, 'topics', '--json']))
  const names = (found ?? []).map(t => t.name).filter(n => typeof n === 'string' && n.length > 0)
  return names.length ? names : [...DEFAULT_TOPICS]
}

// The module's own: started over on a reload, which is fine for all four.
let busy = false
let polling = false
let engine: string | null = null
/** Whether locks may have a moment to compact to hand over: it decides only at a
 * turn's end, so the mod asks after one (and once after a reload), and again
 * only while locks says a decision waits. */
let compactionDue = true
/** locks' pulse file, as `locks endings` names it, and the change of it last seen. */
let pulsePath: string | null = null
let pulseSeen = 0

async function refresh($: EngineInterface): Promise<void> {
  const cwd = await $.session.cwd()
  const found = await board($, cwd, await $.session.id())
  $.ui.status(statusLine(found?.counts, found?.running ?? []))
}

/** Hand the session what ended while it was idle: claimed once, so the Stop
 * hook and the next prompt never deliver it again. */
async function wake($: EngineInterface, enabled: boolean): Promise<boolean> {
  if (!enabled || busy) return false
  const session = await $.session.id()
  const found = json<{ notice: string | null; ids: number[]; statuses?: Record<string, string | null>; pulse?: string }>(
    await locks($, ['endings', '--session', session, '--json']))
  if (found?.pulse) pulsePath = found.pulse
  if (!found?.notice) return false
  $.ui.toast(endedToast(found.ids, found.statuses))
  await $.prompt.submit({ text: found.notice })
  return true
}

/** The Claude Code version the session runs on: locks learns per version
 * whether a `/compact` the mod submits runs as the command. */
async function version($: EngineInterface): Promise<string> {
  if (engine === null) {
    try {
      engine = (await $.session.version()).version || 'unknown'
    } catch {
      engine = 'unknown'
    }
  }
  return engine
}

/** locks' moment to compact, taken once: the line shown to the person, or,
 * with `compact auto`, the engine's own compaction with the line's instructions
 * while no turn runs. What it answered is told to locks, which learns from it
 * (and from its hooks) whether this build compacts on a mod's word. */
async function compaction($: EngineInterface): Promise<void> {
  if (busy) return
  compactionDue = false
  const session = await $.session.id()
  const engineVersion = await version($)
  const step = json<CompactionStep>(
    await locks($, ['compaction', 'next', '--session', session, '--version', engineVersion, '--json']))
  if (!step) return
  compactionDue = step.wait === true
  if (step.said) $.ui.toast(step.said, { timeoutMs: SUGGEST_MS })
  if (step.action === 'advise' && step.advice) {
    // `compact orchestrator`: locks saw a moment; the session decides, and compacts
    // with the `compact` tool if it judges so. A turn that started first keeps it
    // for the next break.
    if (!busy) await $.prompt.submit({ text: step.advice })
    return
  }
  if (step.action === 'continue' && step.text) {
    // A compaction the session asked for finished, and it left itself what to go
    // on with: with no work of its own running, no ending would start its turn.
    if (!busy) await $.prompt.submit({ text: step.text })
    return
  }
  if (!step.line || step.decision === undefined || !step.action) return
  const decision = String(step.decision)
  const tell = (outcome: string[]) => locks($, ['compaction', 'delivered', decision, '--session', session,
    '--version', engineVersion, ...outcome, '--json'])
  if (step.action === 'submit' && !busy) {
    try {
      // The call `/compact` makes, between turns: the engine refuses a `/` text a
      // mod submits, and this never reaches the model as a message.
      const result = await $.session.compact({ instructions: step.instructions ?? '' })
      // Look again at once: a prompt the session left to follow its compaction is due now.
      compactionDue = true
      if (result.skip === undefined) {
        $.ui.toast(compactedToast(step.decision))
        await tell(['--ran'])
      } else {
        $.ui.toast(skippedToast(step.decision, result.skip), { timeoutMs: SUGGEST_MS })
        await tell(['--skipped', result.skip])
      }
      return
    } catch (err) {
      // A headless session (an SDK host, the app's cloud sessions) has no
      // compaction between turns: there `/compact` runs as the person would type
      // it. The submit stays open, and locks settles it from the hooks that see
      // the compaction carry its marker, as it does a submit nobody answered.
      try {
        await $.command.run({ command: 'compact', args: step.instructions ?? '' })
        compactionDue = true
        $.ui.toast(commandToast(step.decision))
        return
      } catch (again) {
        // A turn started, or both calls failed for a reason the mod cannot tell
        // from a build without them: nothing is learned of the build, and the
        // line is the person's to paste.
        await tell(['--rejected', `${String(err)}; /compact: ${String(again)}`])
      }
    }
  } else if (step.action === 'submit') {
    // A turn started while locks answered: the line is the person's to paste,
    // and locks must not read a line nobody submitted as one this build ignored.
    await locks($, ['compaction', 'delivered', decision, '--session', session])
  }
  $.ui.toast(suggestToast(step.line), { timeoutMs: SUGGEST_MS })
}

async function poll($: EngineInterface, wakes: boolean): Promise<void> {
  if (polling) return
  polling = true
  try {
    if (!(await wake($, wakes)) && compactionDue) await compaction($)
    await refresh($)
  } finally {
    polling = false
  }
}

/** A task ended since the pulse was last seen: poll now, not at the next POLL_MS. The first
 * look only learns where the pulse stands; an older locks names no pulse, and polls alone. */
async function pulse($: EngineInterface, wakes: boolean): Promise<void> {
  if (!pulsePath || busy || polling) return
  const stat = await $.fs.stat(pulsePath).catch(() => undefined)
  if (!stat || stat.mtimeMs <= pulseSeen) return
  const first = pulseSeen === 0
  pulseSeen = stat.mtimeMs
  if (!first) await poll($, wakes)
}

/** Whether the person delegated the asks for `queue` and `resume`: read at each
 * check, so turning it off holds at once. A locks that cannot say leaves the ask
 * with the person. */
async function delegated($: EngineInterface): Promise<boolean> {
  const ran = await locks($, ['config', 'delegate'], { timeoutMs: 10_000 })
  return ran.ok && ran.out === 'true'
}

/** Work started and not awaited (a refresh after a call) never fails the hook that started it. */
function ignore(): void {}

/** A tool hook that threw answers for itself: the model reads why, never a hang. */
function failed(): { deny: string } {
  return { deny: 'locks: the tool failed inside the mod; `locks` through Bash still works' }
}

export const register: Register = (on, options) => {
  const wakes = options.wake !== false

  on('session.start', async ($, e, next) => {
    const answer = await next(e)
    const cwd = await $.session.cwd()
    const names = await topics($, cwd)
    await $.tool.register({
      name: 'recall',
      description: "Read what this project's notes say: business rules, how the code works, decisions and why, "
        + 'conventions, how it is run. Use it before rebuilding context from the code, above all after a '
        + 'compaction. A note marked TO REVIEW describes code that changed since: check it.',
      inputSchema: {
        type: 'object',
        properties: {
          question: { type: 'string', description: 'what you want to know; empty for the latest notes' },
          topic: { type: 'string', description: `only this topic: ${names.join(', ')}` },
          path: { type: 'string', description: 'notes about this file, relative to the checkout' },
        },
      },
    })
    await $.tool.register({
      name: 'note',
      description: "Keep a durable fact about this project in its notes, for whoever works here next: one the "
        + 'person told you or you confirmed. Not progress, not a guess, never a secret.',
      inputSchema: {
        type: 'object',
        properties: {
          fact: { type: 'string', description: 'one or two self-contained sentences, in the person\'s language' },
          topic: {
            type: 'string',
            description: `where it is filed: ${names.join(', ')}; left out, the topic its words point to`,
          },
          title: { type: 'string' },
          anchors: { type: 'array', items: { type: 'string' }, description: 'files it is about' },
          links: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                to: { type: ['integer', 'string'], description: 'the other note\'s id' },
                kind: { type: 'string', description: LINKS.join(', ') },
              },
            },
          },
        },
        required: ['fact'],
      },
    })
    await $.tool.register({
      name: 'queue',
      description: 'Hand a task to a locks worker: it is classified, routed to a model and effort, run in its own '
        + 'worktree and checked. Returns at once; when it ends, its notice reaches this session on its own. '
        + 'Write the task so a stranger could do it: the worker sees nothing of this conversation.',
      inputSchema: {
        type: 'object',
        properties: {
          task: { type: 'string' },
          verify: { type: 'string', description: "the repository's own check, e.g. npm test" },
          kind: {
            type: 'string',
            description: `the kind you read the task as: ${KINDS.join(', ')}. Given, locks starts the worker `
              + 'without classifying it again (a model call of a few seconds); left out, locks classifies the task',
          },
        },
        required: ['task'],
      },
    })
    await $.tool.register({
      name: 'tasks',
      description: "This project's locks work: what waits on the person and why, what runs, what is queued.",
      inputSchema: { type: 'object', properties: {} },
    })
    await $.tool.register({
      name: 'resume',
      description: 'Continue a locks task that stopped, in the background, from its kept work. Refused when only '
        + 'the person can clear what stopped it (a refused command, an approval): tell them instead.',
      inputSchema: {
        type: 'object',
        properties: { id: { type: ['integer', 'string'] }, max_turns: { type: ['integer', 'string'] } },
        required: ['id'],
      },
    })
    await $.tool.register({
      name: 'overview',
      description: "Every Claude Code session locks saw lately, in every project, and how its work stands: what "
        + 'waits on the person and why, what runs (attempt, model, since when), what is queued, what ended. For '
        + 'watching sessions in several projects from one place; it reads, and each session still hears its own endings.',
      inputSchema: {
        type: 'object',
        properties: { hours: { type: ['integer', 'string'], description: 'sessions seen and tasks ended this recently; 24 when left out' } },
      },
    })
    await $.tool.register({
      name: 'compact',
      description: 'Compact this session, on your judgment: when the work at hand no longer needs this '
        + "conversation's detail (a break between tasks, a large context), not in the middle of work that still "
        + "needs it. It runs when this turn ends, told to keep the person's requests verbatim, the open locks work "
        + 'and the branches waiting to merge; locks puts its digest of the session back after it. With nothing of '
        + 'yours running, no ending starts your next turn: say in `then` what to go on with, and once the '
        + 'compaction finished it reaches you as your own prompt. locks also tells you when it sees a good moment; '
        + 'the call is yours.',
      inputSchema: {
        type: 'object',
        properties: {
          why: { type: 'string', description: 'why now, in one line' },
          keep: { type: 'string', description: 'what the summary must also keep, beyond what locks keeps itself' },
          then: {
            type: 'string',
            description: 'your prompt to yourself once the compaction finished: what you go on with, as you would '
              + 'ask it (left out, the session waits for the person or an ending)',
          },
        },
        required: ['why'],
      },
    })
    await $.command.register({ name: 'locks', description: "locks' board: what waits on the orchestrator, runs and is queued" })
    $.clock.every(POLL_MS, () => {
      void poll($, wakes).catch(ignore)
    })
    $.clock.every(PULSE_MS, () => {
      void pulse($, wakes).catch(ignore)
    })
    void refresh($).catch(ignore)
    return answer
  })

  // A turn starts in the main conversation only; a subagent's turns end inside
  // it (`turn.complete` with an `agentId`) and leave it running.
  on('turn.start', ($, e, next) => {
    busy = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const answer = await next(e)
    if (e.agentId === undefined) {
      busy = false
      compactionDue = true
      void refresh($).catch(ignore)
    }
    return answer
  })

  on('tool.call', { tool: RECALL }, async ($, call) => {
    const e = input(call)
    const args = ['recall', text(e.question), '--repo', await $.session.cwd()]
    if (text(e.topic)) args.push('--topic', text(e.topic))
    if (text(e.path)) args.push('--path', text(e.path))
    const ran = await locks($, args)
    return ran.ok ? { result: ran.out || 'no notes match' } : { deny: ran.err || 'locks recall failed' }
  }).catch(failed)

  on('tool.call', { tool: NOTE }, async ($, call) => {
    const e = input(call)
    const fact = text(e.fact)
    if (!fact) return { deny: 'a note needs a fact' }
    const args = ['note', fact, '--repo', await $.session.cwd(), '--json']
    // A topic is resolved by locks, aliases included; one it does not know is
    // answered with the project's topics.
    if (text(e.topic)) args.push('--topic', text(e.topic))
    if (text(e.title)) args.push('--title', text(e.title))
    for (const path of Array.isArray(e.anchors) ? e.anchors : []) {
      if (text(path)) args.push('--anchor', text(path))
    }
    const skipped: string[] = []
    for (const link of Array.isArray(e.links) ? e.links : []) {
      const to = whole((link as { to?: unknown } | null)?.to)
      const kind = text((link as { kind?: unknown } | null)?.kind)
      if (to !== null && LINKS.includes(kind)) args.push('--link', `${to}:${kind}`)
      else skipped.push(JSON.stringify(link))
    }
    const ran = await locks($, args, { session: await $.session.id() })
    const kept = json<{ id: number; new: boolean; topic: string; title: string }>(ran)
    if (!kept) return { deny: ran.err || 'locks note failed' }
    const said = `${kept.new ? 'kept' : 'already known as'} #${kept.id} [${kept.topic}] ${kept.title}`
    return {
      result: skipped.length
        ? `${said}. Links left out (a link is {to: <note id>, kind: ${LINKS.join('|')}}): ${skipped.join(', ')}`
        : said,
    }
  }).catch(failed)

  on('tool.call', { tool: QUEUE }, async ($, call) => {
    const e = input(call)
    const task = text(e.task)
    if (task.length < 8) return { deny: 'write the task in full: the worker sees nothing of this conversation' }
    const args = ['queue', 'add', task, '--repo', await $.session.cwd(), '--json']
    if (text(e.verify)) args.push('--verify', text(e.verify))
    // A kind locks does not have is left to its classifier, not a refusal: the
    // task is what matters, and the model hears what it named instead.
    const kind = text(e.kind)
    if (KINDS.includes(kind)) args.push('--kind', kind)
    // A request that is several tasks is split before anything is queued, and the split may ask Haiku:
    // past the default minute the run was cut off with nothing queued and nothing said of why.
    const ran = await locks($, args, { session: await $.session.id(), timeoutMs: QUEUE_MS })
    const queued = json<{ id: number; title: string; then: string; tasks?: { id: number; title: string }[] }>(ran)
    if (!queued) return { deny: ran.err || 'locks queue add failed' }
    void refresh($).catch(ignore)
    const unknown = kind && !KINDS.includes(kind)
      ? ` The kind "${kind}" is not one of locks' (${KINDS.join(', ')}); it classifies the task itself.`
      : ''
    if (queued.tasks && queued.tasks.length > 1) {
      const pieces = queued.tasks.map(t => `#${t.id} — ${t.title}`).join('; ')
      return {
        result: `split into ${queued.tasks.length} tasks and queued: ${pieces}. ${queued.then} Their endings reach this session on their own.${unknown}`,
      }
    }
    return {
      result: `queued #${queued.id} — ${queued.title}. ${queued.then} Its ending reaches this session on its own.${unknown}`,
    }
  }).catch(failed)

  on('tool.call', { tool: TASKS }, async $ => {
    const found = await board($, await $.session.cwd())
    if (!found) return { deny: 'locks could not read its board' }
    const lines: string[] = []
    for (const card of found.needs_you) {
      lines.push(card.waits_on === 'orchestrator'
        ? `yours to decide (resume, rewrite or dismiss with why): #${card.id} [${card.status}] ${card.title}`
        : `ask the person in this conversation (only they can grant it), then run its line: #${card.id} [${card.status}] ${card.title}`)
      if (card.stop) {
        lines.push(`  stopped by ${card.stop.who}: ${card.stop.reason}`)
        lines.push(`  ${card.stop.todo}${card.stop.next ? `: ${card.stop.next}` : ''}`)
      } else if (card.asks) {
        lines.push(`  ${card.asks}`)
      }
    }
    for (const card of found.running) {
      lines.push(`running: #${card.id} ${card.title}${card.current_cell ? ` (${card.current_cell})` : ''}`)
      if (card.progress_line) lines.push(`  now: ${card.progress_line}`)
      if (card.files_line) lines.push(`  ${card.files_line}`)
      if (card.slow) lines.push(`  slow: ${card.slow}`)
    }
    for (const lane of found.queued) {
      for (const card of lane.tasks) lines.push(`queued: #${card.id} ${card.title}${lane.paused ? ` (lane paused: ${lane.reason ?? ''})` : ''}`)
    }
    return { result: lines.join('\n') || 'nothing waits, runs or is queued here' }
  }).catch(failed)

  on('tool.call', { tool: RESUME }, async ($, call) => {
    const e = input(call)
    const id = whole(e.id)
    if (id === null) return { deny: 'resume needs a task id: the number after # on the board (`tasks`)' }
    // `--unattended`: locks refuses what only a person may clear. The model's
    // input names no rule; nothing it passes reaches `--allow`.
    const args = ['resume', String(id), '--unattended', '--detach']
    const turns = whole(e.max_turns)
    if (turns !== null) args.push('--max-turns', String(turns))
    const ran = await locks($, args)
    if (!ran.ok) return { deny: ran.err || `locks could not resume #${id}` }
    void refresh($).catch(ignore)
    return { result: ran.out }
  }).catch(failed)

  on('tool.call', { tool: COMPACT }, async ($, call) => {
    const e = input(call)
    const args = ['compaction', 'request', '--session', await $.session.id(), '--cwd', await $.session.cwd(),
      '--why', text(e.why), '--json']
    if (text(e.keep)) args.push('--keep', text(e.keep))
    if (text(e.then)) args.push('--then', text(e.then))
    const ran = await locks($, args)
    const asked = json<{ id?: number; error?: string }>(ran)
    if (!asked?.id) return { deny: asked?.error || ran.err || 'locks could not take the compaction' }
    // Taken when the session is idle: the next look after this turn ends.
    compactionDue = true
    return { result: `compaction #${asked.id} is set: it runs when this turn ends. Finish the turn with what the `
      + 'person needs to know now; after it, locks puts its digest of the session back'
      + (text(e.then) ? ', and your `then` reaches you as your next prompt.' : '.') }
  }).catch(failed)

  on('tool.call', { tool: OVERVIEW }, async ($, call) => {
    const hours = whole(input(call).hours)
    const ran = await locks($, ['overview', '--hours', String(hours !== null && hours > 0 ? hours : 24)])
    return ran.ok ? { result: ran.out } : { deny: ran.err || 'locks overview failed' }
  }).catch(failed)

  // Only an `ask` turns into an allow: a rule or a setting that denies one of
  // these still denies it, and an organization's ceiling still caps it.
  for (const tool of QUIET) {
    on('tool.check', { tool }, async ($, e, next) => {
      const verdict = await next(e)
      return verdict.decision === 'ask'
        ? { decision: 'allow', reason: "locks: reads this project's notes and board, or keeps a note" }
        : verdict
    })
  }
  for (const tool of DELEGATED) {
    on('tool.check', { tool }, async ($, e, next) => {
      const verdict = await next(e)
      return verdict.decision === 'ask' && await delegated($)
        ? { decision: 'allow', reason: 'locks: the person delegated starting locks work to the orchestrator '
            + '(`locks config delegate off` puts it back to them)' }
        : verdict
    })
  }

  // The board is the web UI: one place to see and press, not a second, poorer
  // copy in the terminal. `locks ui` serves until stopped, so it is started
  // detached; when one already serves, it says where and exits. The person typed
  // `/locks`, so it opens once; no tab for each piece of work, the page shows them all.
  on('command.run', { command: 'locks' }, async $ => {
    const bin = `${$.plugin.root}/bin/locks`
    try {
      await $.process.run(['/bin/sh', '-c', 'nohup "$0" ui --open >/dev/null 2>&1 &', bin], { timeoutMs: 10_000 })
    } catch (error) {
      return { text: `locks: the board did not start (${String(error)}); run \`locks ui --open\`` }
    }
    return { text: `locks board: ${BOARD_URL}` }
  })
}
