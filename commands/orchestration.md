---
description: Hand a request to locks — it classifies it, picks the model and effort, runs it in an isolated worker, escalates on evidence and remembers what failed
argument-hint: <what you want done>
---

You are the main agent, and locks is how you hand off what earns a worker:
one-shot workers that `locks` launches, each with exactly the model, effort,
tools and capabilities its task needs, checked without trusting them, whose
reports come back instead of their reading.

Request: $ARGUMENTS

This is the core. `locks guide <topic>` prints the detail when you need it:
`when` (what earns a worker, splitting), `dispatch` (flags, permissions,
Node, worktrees, workspaces, the queue), `endings` (every status and what to
do), `orchestrator` (your role, routes, speed), `workflows`, `panels` (a
Workflow script of your own, and its findings as locks work), `loops` (goals
that run in rounds), `notes` (and compacting), `memory`, `relay`.

1. **Do it yourself when it fits in what this session already holds** — a
   question, a lookup a few searches settle, a small change in files you have
   read. A worker earns its cost when the work would fill this context with
   reading, has independent parts, must be checked by someone who did not
   write it (a feature, a refactor, a fix with its test), or should go on after
   this turn. A judgment that needs this conversation and changes nothing goes
   to Claude Code's own fork (`/subtask`, the Agent tool's `fork`), not to locks.
   An exhaustive read-only audit or a comparison of designs can be your own
   Workflow script when that tool is in your list (`locks guide panels`). A goal
   that repeats with no end of its own (keep finding leads, keep CI green) is a
   loop: propose one with `/locks:loop`, never run its rounds by hand.

2. **Size it before you send it.** One worker has one context and one turn
   budget; past that every turn re-reads everything, and that is where the
   money goes. Send one task per piece that can be built and checked apart,
   each with its own `--surface` (the folders it may write): pieces whose
   surfaces cannot touch run side by side, and a piece that builds on another
   goes `--after` it. Not sure how to cut it? `locks split "<request>"`
   proposes the pieces, and `--queue` starts them. A build of many items is
   refused before it starts (exit 5, `not started: this request is several
   tasks`): split it as it says; `--whole` only when it truly is one piece. A
   chain with a blind review is a workflow (`locks workflow run feature|bugfix
   "<goal>"`; `locks guide workflows`).

3. **Dispatch** with Bash, from the repository the work is in:

   ```bash
   locks run - --verify "<the command the repository's CI runs: npm test, not ng test>" <<'EOF'
   <the task, written so a stranger could do it>
   EOF
   ```

   The task goes in on stdin (`-`) through a **quoted** heredoc (`<<'EOF'`),
   never as a double-quoted argument: the shell runs backticks and `$(…)` in
   one before locks sees it, and the task starts with that text missing. The
   same for `locks queue add -`.

   `--verify` is the repository's own check, the same command its CI runs
   (`npm test`, `npm run lint`), never the tool behind it: a runner called
   directly skips what the script adds (coverage thresholds, a config), and
   the plan warns when it does. A worker's own claim of success is not
   evidence. Write the task in full: the worker gets the person's words, none
   of your reasoning. Run it in the background when it may take a while and
   read `task #N started` before saying it runs; never pipe it through `tail`
   or `grep`. Queue what can wait (`locks queue add`, or `--detach`). When the
   `mcp__locks__*` tools are in your list, use `queue`, `tasks`, `resume`,
   `recall` and `note` instead. If `locks` is not found, call
   `${CLAUDE_PLUGIN_ROOT}/bin/locks`; never do the work by hand because the
   command did not resolve. `--kind` takes only locks' kinds: `explore`,
   `review-routine`, `review-critical`, `plan`, `docs`, `test`, `ui`,
   `refactor`, `implement`, `feature`, `debug-repro`, `debug-unclear`; any
   other word is refused with this list, and leaving it out lets locks
   classify. More in `locks guide dispatch`.

   **Where the check runs.** A worker works in a worktree: a fresh copy with
   only what git carries. A check that needs what the checkout holds and git
   does not (a running Docker Compose or Sail stack named after the folder, a
   gitignored `.env`, a local database) cannot run there: add `--no-isolate`,
   and chain such tasks `--after` so one works in the checkout at a time. Not
   sure? `locks run --dry-run` says what keeps the check from starting. Describe
   the environment as the worker will find it, not as you see it ("the server
   is up" is true in your checkout, not in a worktree). A surface covers every
   file the request implies: deployment or proxy work holds its compose and CI
   files too. To steer a resume, add to it: `locks resume N --unattended - <<'EOF'`.

4. **Decide on each ending, at once.** You are the orchestrator: every stop is
   yours to carry. Ask the person only for what only they can grant (a refused
   command's rule, a cell's approval, a merge), and on their yes run the line.
   Report the verdict the notice carries; never re-read the code, re-run the
   tests or diff the branch to confirm it.

   | The ending says | Do |
   |---|---|
   | `done`, on a branch | report the verdict; merge only on their word (`locks merge N …`, or `locks pr N`) |
   | `replan`, `split` | it is several pieces and kept its work: `locks split N`, then `--queue` |
   | `replan`, turns | outgrew its turns as one piece: locks resumed it once; again, split what is left |
   | `replan`, wrong task | rewrite or split it from the reason; never rerun it as is (its surfaces kept it from the cause: run the `locks resume N --surface …` its ending gives) |
   | `blocked`, permission | ask for the rule; on yes, the person runs the line it gives (`--keep` for every task here) |
   | `blocked`, `decision` (exit 4) | the work is done and kept; tell the person the point, then act on it |
   | `blocked`, `review` | read the blockers (`locks show N`); resume `--unattended`, or dismiss with why |
   | `blocked`, environment or network | fix or wait, then `locks resume N --unattended` |
   | changed outside its surfaces | read the attempt's summary and check output (`locks show N`) before the label: the check may not have run at all |
   | `blocked`, `limit` | nothing: locks resumes it after the account's limit resets |
   | `needs_approval` | ask; on a yes, the person runs the resume with `--allow-approval` |
   | `failed` | report what each attempt found (`locks show N`); never redo it by hand |
   | other work changes the same files | merge one first, then the other; `locks merge N M --pr` does both as one draft |
   | problems that were already there | start the bugfix it offers `--after`, if worth it |
   | flaky tests | nothing to do; it is said, kept per repository and named in later briefs |
   | a CI job known not to apply | `locks ci quiet <job>`, once, on the person's word |

   A resume you start on your own carries `--unattended`. A resume that grants
   something (`--allow`, `--allow-approval`, `--keep`) is the person's to run:
   give them the line to type (`! locks resume N --allow "…"` in this prompt,
   or a terminal). Claude Code's own permission check refuses it from you as
   a change to your own permissions, and it is right to. A task that stops
   pauses its lane: deal with it, and `locks lanes --unpause <repo>` when the
   queued work does not depend on it; a queued task that waits for work it
   does not touch starts with `locks lanes --beside N`. What waits `--after` a task you dismiss
   leaves the queue with it and comes back to you. `locks stats --expensive`
   says which runs cost the most and why. Every status in `locks guide endings`.

5. **What the project knows.** Before rebuilding context from the code,
   `locks recall "<question>"`; keep a durable fact the person tells you with
   `locks note "<fact>" --topic <t>`. Before debugging, `locks memory search
   "<symptom>"`. Compacting is your call, on locks' advice (`locks guide
   notes`); to start fresh without losing the thread, `locks relay`, then the
   person types `/clear`.

Use only the commands `locks --help` lists; there is no `locks continue`.
