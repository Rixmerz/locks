# Observability and measurement

How locks shows what it really did, and how a change to locks is measured. Everything here only
reads, except where marked as the person's opt-in.

## The trace (`trace`, off by default)

`traces/<session>.jsonl` in the locks home holds every hook event (`hook:<Event>`), every tool call
with its input and output (`tool:<name>`; `livespec_tool` marks livespec's tools), the context a
hook injected (`inject:<Event>`) and every event locks recorded on the session's tasks
(`locks:<event>`). The session's transcript is copied beside it at each Stop. Secrets are masked.
Read it with `locks trace`, or audit it with the `insight` skill.

## Forensics (`locks forensics`)

Reads a copy of the store, never the live file: a backup through SQLite, or a copy passed with
`--db`. Every figure is observed:

- spend by kind and cell, and the dollars of attempts that did not pass
- the share of input the cache served, by model
- tool friction: calls that failed, and calls repeated back to back with the same arguments
- brief footprint growth
- the notes-fork trial
- what the trace shows across its sessions

It calls no model and uses no network.

## Brief footprint (`locks footprint`)

Every `briefed` event records its footprint with the release that wrote it: the characters of the
context locks put before the task, of the role's system prompt, and the number of skills carried.
The report gives the median per kind and release, and names a release that made a kind's brief
`footprint.GROWTH` times heavier than the one before. It is a warning, never a stop.

## Skill use and lifecycle (`locks skills`)

For each skill: the briefs that carried it, the pass-first of those tasks, and when it was last
carried. Its state is:

- `active`: carried within `skill_use.STALE_DAYS`
- `stale`: not carried since then, up to `ARCHIVE_DAYS`
- `dormant`: not carried for longer
- `pinned`: `locks skills --pin NAME`, kept in the locks home

A dormant skill is reported and nothing else. locks never moves, archives or deletes a role's
skill: only a person changes those files.

## Notes from a fork of the session (`notes_fork`, off by default)

A session's notes can be filed by a fork of the session (`claude -p --resume <id> --fork-session`,
no tools, no session kept) instead of by Haiku. The fork already holds the conversation and may
read it from the cache. The two arms alternate per project until each has `notes_fork.TRIAL`
filings. After that, the fork stays only where it kept no fewer facts per filing.

Each filing's cost is kept and shown in `locks forensics`, but it is never the criterion. The
setting is off by default because a fork runs on the session's model.

## A/B of two checkouts (`bench/ab`)

Trap tasks run through the base and the head checkout, judged by the store's records and a hidden
check. See `bench/ab/README.md`. It spends real money: run it on purpose.
