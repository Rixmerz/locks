---
description: Propose, build and run a locks loop — a goal that runs in rounds through the night (finding and following up leads, keeping CI green, triage) until evidence stops it — after the person agrees to every part of it
argument-hint: [the goal: what should keep happening, and where]
disable-model-invocation: true
---

A locks loop is for a goal that does not end with one answer: work that repeats,
learns from each round and stops on evidence. Each round is an ordinary locks
workflow run (fresh workers, checked, costed); the loop keeps a diary between
rounds, measures its metric itself, tries strategies against each other, and
holds every tool that acts on the world to the person's rules. Detail: `locks
guide loops`.

Request: $ARGUMENTS

## 1. Is it a loop?

It is when the goal repeats with no natural end (keep doing X until Y, every
night, follow up on what came back) and something can be measured between
rounds. A task with one result is `locks run`; a chain that ends is a
workflow. Say which it is, and why, before going on.

## 2. Ask the person, in their language, one short round

Ask only what you cannot read from the request, the repository or locks:

- **The goal and its metric.** What counts as progress, and a command that
  prints it as a number (rows in a file the rounds keep, open CI failures,
  replies counted by a script). No command, no metric: the loop then stops
  only on its window or on the person, and say so.
- **What a round does**, step by step: that is its workflow.
- **What the workers need to know** that is not in a repository: what may be
  offered or promised, prices, what is off limits, the tone. These are the
  loop's rules (`--policy`), in the person's words, one rule each. Never invent
  a price, a service or a promise: ask.
- **What it may do alone and what waits for them.** A tool that sends, posts or
  replies acts on the world: the loop runs as a draft first (nothing leaves),
  and only the person's `locks loop act` lets it act, within the hours
  (`--hours 8-21`), an hourly cap (`--per-hour`) and exclusions (`--exclude`,
  who must never be contacted) that locks' guard enforces. Anything outside
  the rules (a discount, a deadline, a deal) is listed for them, never done.
- **Where and when:** the folder its rounds run in (a folder of its own when
  the work is not code), how often (`--every 30m`), until when (`--until 07:00`).
- **Strategies to compare**, if they have two or more (`--variant`): the loop
  measures which one moves the metric and adopts it.

Check the tools the rounds need: `locks` hands a worker only the MCP servers of
the person's registry (`~/.local/share/locks/capabilities.json`) and adopted
neighbours. A tool that is not there is said, with what entry it needs; one that
sends is registered with `"outbound": {"acts": ["<its sending tools>"]}`, so the
guard holds it. Never write the registry, a settings file or a credential for
them: show the entry and let them add it. A channel whose terms forbid
automation (a social network's messages) is for reading or drafts only: say so.

## 3. Propose the whole loop, then wait

Show, in a few lines: the workflow (its steps, the file it will be), the
`locks loop add` line with every flag, the rules as they will be kept, the
metric and the stop rules (target, window, `--stale` rounds without
improving), that it starts as a draft, and the cost of a round estimated from
locks' own numbers (`locks stats`), never capped. Nothing is written before
the person says yes; change what they change and show it again.

## 4. Build it with locks' commands only

On their yes:

```bash
locks workflow new <round> --description "…"   # then locks workflow step add|set …
locks loop add <name> --goal "<their words>" --workflow <round> --dir <folder> \
  --every 30m --until 07:00 --metric "<command>" [--target N] [--down] \
  --policy "<rule>" … [--variant "<strategy>" …] [--hours 8-21] [--per-hour N] [--exclude "<who>"]
locks loop start <name>
```

A round's steps keep their state in files of the loop's folder (a list of
leads and what was said to each, a log): tell each step's prompt which file it
reads and which it updates, so the next round starts where this one ended. A
step that updates them writes (`--kind implement`), never a reading kind.

## 5. Afterwards

It runs without this session. When it stops (target, window, no progress, or
something only the person clears), this session hears of it once with its
summary: tell the person what it did, what it cost and what waits on them
(`locks loop show <name>`). Moving from draft to acting is their word only:
show them the draft's rounds first, and run `locks loop act <name>` only when
they say so. `locks loop stop <name>` ends it after the round running.
