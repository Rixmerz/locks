# Chains of steps: when to run a workflow, and how a run goes on by itself

**Cut work where context changes hands, never by role.** The phases of one
task (plan, build, test; reproduce and fix) are one worker's: the one that
builds a feature writes its tests. What earns its own step is what must not
share that context: a review, which reads blind (the request and the diff),
and fixing what a review confirmed. That chain is a workflow, not one you
relay: run `locks workflow list` and pick the one that fits (`feature`,
`bugfix`, `review-fix`, `dual-review`, `panel`, `drift`, `design-review` for a design document), or the one the person named. Then:

```bash
locks workflow run <name> "<the goal, written so a stranger could do it>"
```

Its steps are ordinary locks tasks that the engine chains by itself. Each
one starts from the commit the step before kept, and is told what the
earlier steps found (a review of a change excepted: it reads blind). Do not wait on a step to start the next one, and do
not pass results along yourself: that is what the run does without you.
You hear of the run once, when it ends or when a step waits on the person.
`locks workflow status <run>` shows where it is. A step that stopped is
continued with `locks resume <task> --unattended` or `locks workflow retry <run> <step>`,
and the run goes on from there. A step that asks the person (`ask`) waits for their answer:
put the question to them in your conversation and run `locks workflow answer <run> <step>
<option>` with what they chose, never an answer of your own. A step that runs another workflow
goes on by that run's steps (`locks workflow status` names it). `locks workflow render <name>
--markdown` draws a workflow as a Mermaid graph to show the person before a run.

**A step can start where a reading step left off.** A step given a `trunk`
(an `explore` or `plan` step before it) starts as a fork of that step's
session: what it read is already in its context, and its own role is its
first message after the fork, since the fork keeps the trunk's system prompt.
Use it where a reading step and the work after it share what must be read
(a plan and the parts it splits; an exploration and the steps that act on
it), never to hand a review an opinion: a review forks only an exploration.
locks measures it in arms per repository and keeps it where it pays.

When no saved workflow fits and the same shape will come back, make one
instead of chaining by hand: `locks workflow new <name> --from <closest>`,
then `locks workflow step add|set|rm`. Tell the person what you saved, and
where. A one-off sequence of two steps is just two `locks run`s.
