# Design

## The decision

One core absorbs what has to share state and decisions; capabilities stay
separate servers that the core hands to the worker that needs them.

The router, the launcher, the attempt trace and the memory are one loop:
classify → launch → check → record → learn. Split across plugins, that loop
became two schedulers, two launchers, two routers and two memories, coupled by
tool names written in prose. Inside one core they are one module each.

Capability servers (a code index, a layout inspector, a tracer) are useful on
their own, keep their own release cadence, and should not be in every worker's
tool list. MCP has no server-to-server channel, which used to mean the core
could only *name* them. With one-shot workers that stops mattering: the core
does not call them, it gives them to the worker (`--mcp-config`,
`--strict-mcp-config`).

## Adopted, not listed

A capability is a server handed to a worker; the core knows nothing of what it
says. An adapter is a neighbour the core *understands*: it reads the
neighbour's data and uses it where the core decides — routing, the brief, the
report. livespec is the first, and it is on by default: locks asks its index itself and
briefs the answers (a dossier: symbols, callers, tests, specs, close helpers) as data, so a
worker starts by working, not by searching. Workers are not handed livespec's tools (handed
to 46 attempts, called in 3); only the independent check keeps the server, behind a read-only guard.

Its code is not copied into locks. livespec is AGPL-3.0 and depends on
tree-sitter, fastmcp and networkx; locks' hooks run on every prompt and must
import with the standard library alone. So locks reads livespec's SQLite index
read-only and drives its CLI, and pins the release it was checked against.

## Why one-shot workers, not subagents

| | subagent | one-shot `claude -p` |
|---|---|---|
| tools | requested in its charter | enforced: `--tools`, `--disallowedTools` |
| MCP servers | inherits the session's | exactly the selected ones |
| model / effort | fixed per charter | per attempt — escalation needs this |
| context | returns into the orchestrator's | dies with the process; only the result block returns |
| where it runs | the session's directory | launched in one directory, working in another |
| cost | no process start | a few seconds of start-up per attempt |

## The orchestrator

The orchestrating session is whatever model the user runs; `/orchestration`
keeps it thin. The loop itself is deterministic code. Models are asked for
two things only: Haiku classifies what the rules did not recognise, and
workers do the work. Judgment about the *next move* is read from typed
evidence (the failure kind, the verify command, whether two cells repeated an
answer), never from a model's confidence in itself.

A Haiku-as-orchestrator was considered and rejected: orchestration is where
noticing matters most (understanding the request, judging a failure), and a
measured run of an orchestrator at low effort skipped its own self-check.

## What came from where

| Piece | Origin |
|---|---|
| failure kinds, retry vs escalate vs replan, repeated-answer detection, attempt history in the brief, result contract, git-read changes | vise's agent runtime |
| prompt ↔ task coupling, follow-ups into the running task, continuation nudges, interrupted turns, dead ends in context | tasky's hooks and ledger |
| classifier with a JSON schema and no tools, run through `claude -p` | tasky's smart search |
| model × effort matrix with a purpose per effort, per-kind ladders, axis chosen by the failure, learned start | new |

## Not yet

- **Calibrate the ladders.** They are starting points. A sweep across real
  tasks per kind, like the one behind vise's routing defaults, should move
  them; the attempt trace is already the data such a sweep reads.
- **Parallel runs.** Independent tasks can already run as separate
  `locks run` processes (each has its own worktree); a planner that splits a
  request into a dependency graph is not here yet.
- **The rest of tasky:** the queue and auto-pull, the dashboard, transcript
  ingest and token accounting, the architecture map.
- **The rest of vise:** gates on a workflow step (validators that must hold
  before the next step starts, beyond the step's own check) and conditional
  edges. Workflows themselves are here (`workflows.py`): saved chains of
  tasks the engine advances, unlike vise's graphs, which the agent itself
  traversed.
- **An MCP surface** for reading tasks and memory from inside a session, kept
  small: reading tools for the orchestrator, authoring tools only in workers.
