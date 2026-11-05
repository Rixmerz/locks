---
description: Watch every session that uses locks, in every project, from this one — how each one's work stands, what ended, and what waits on the person
---

This session is a watcher: the person works with locks in several sessions, one
per project, and wants one place to see how each is going and to send them
work. You read every session's locks work; each session still runs its own.

## See how each session stands

Workflow runs show there too, each with the state of every step: a run goes
on by itself, so watch it rather than starting its steps. `locks workflow
status <run>` shows one in depth.

Call the `overview` tool (or run `locks overview`; `--hours N` widens or narrows
the window, `--json` for fields). It lists every session locks saw lately, with
its directory and last prompt, and under it each task it sent: what waits on the
person and why, what runs (attempt, model and effort, since when), what is
queued, what ended. Work no listed session sent comes last.

Report it per session, in a few lines each: what moved since the last look,
what waits on the person and what they would do about it. Lead with what waits.
For one task in depth: `locks show <id> --json`.

## Hear when work ends

Watch the endings of every project as they happen, from now on:

```bash
locks events --follow --new --kind finished --json
```

Run it with the Monitor tool when you have it, so each line wakes you;
otherwise with `run_in_background`, and read its output when you look again.
On each ending, run `locks overview` for the session it belongs to and tell the
person what it means.

## Send a session work

List the sessions with `ListAgents` and send one a prompt with
`SendMessage({to: "<name>", message: "..."})`. Its names are Claude Code's,
not locks': match them to `overview` by the session's directory, which the
name usually starts with. When two could be the one, ask the person.

A prompt you send is the person's word in that project, so:

- Send what the person asked for, written in full: the session sees nothing of
  this conversation.
- A message to a busy session waits for its turn; say what it should do when it
  reads it, not what was true when you sent it.
- What only a person may clear stays theirs: a refused command, an approval, a
  branch to merge, a dismissal. Tell them; do not ask a session to do it for them.

## Hear the endings of work you asked for

Endings go to the session that sent the work. When you ask a session to send
work, ask it to pass `--notify <your session id>` to `locks run` or
`locks queue add` (your id is `$CLAUDE_CODE_SESSION_ID` in your shell): you
then get a copy of that task's ending at your next turn, marked as a copy,
while that session still gets its own and acts on it. Decide with it through
`SendMessage`; the copy gives you no claim on the task.

## Never

- `locks endings` without `--peek`: an ending is delivered once, and it
  belongs to the session that sent the work. Reading it here takes it from
  that session.
- `locks resume`, `dismiss` or `cancel` on another project's work unless the
  person asked for that task: the session that sent it is acting on it.
- Running work yourself in a project another session owns: send it to that
  session, or queue it there with `locks queue add --repo <dir>` when the
  person asked for that.
