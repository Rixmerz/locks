---
description: Make, change, copy, run and watch locks workflows — saved chains of tasks that go on by themselves, step after step
argument-hint: [what the person wants: run X on Y, make a workflow for Z, change step S, where is run N]
---

A locks workflow is a saved list of steps. Each step is a task text with the
steps it waits on. A run queues the first steps. When a step passes, locks
queues the steps that waited on it, on the commit it kept, and tells them what
it found. You start a run and watch it; you never relay a step's result to
the next one.

Request: $ARGUMENTS

## Find one

```bash
locks workflow list              # every workflow, nearest scope first
locks workflow show <name>       # its steps, inputs and where it is saved
```

There are three scopes, and the nearest one wins:

- `project`: `.locks/workflows/` in this checkout, shared through the repository.
- `user`: the person's locks home.
- `bundled`: the templates `feature`, `feature-parallel`, `bugfix`, `review-fix`,
  `dual-review`, `panel` (four blind reviews, one lens each) and `drift` (a read-only
  comparison of the documents in a folder with the code, `--input dir=docs/designs`). `feature-parallel` is for a feature that touches several
  areas; with a plan of one part it is `feature`.

A workflow in a nearer scope hides one of the same name further away.

## Make or change one

Start from the closest existing workflow rather than from nothing:

```bash
locks workflow new <name> --from <existing> [--scope project|user]
locks workflow copy <source> <target>
locks workflow new <name> --description "..."      # one empty step to fill
```

Change it one step at a time. Each change is checked before it is written:

```bash
locks workflow step add <name> <step> --prompt "..." [--after <step>] [--needs a,b] \
    [--kind <kind>] [--role <role>] [--surfaces 'src/api/**,docs/**'] [--verify "<check>"] [--start <model/effort>] [--max-turns N] \
    [--memory note:12,topic:zones,problem:7]
locks workflow step set <name> <step> --prompt "..." [--clear verify]
locks workflow step rm <name> <step>
locks workflow validate <name>
locks workflow rm <name> --scope user|project
```

To rewrite a whole definition, get it with `locks workflow show <name> --json`,
change the JSON, and write it back with `locks workflow save <file> --replace`.
`locks workflow edit <name>` opens it in the person's editor.

`locks workflow show` prints the definition's revision. Pass it back with
`--expect <revision>` on `save`, `step add|set|rm` and `edit`: the change is
refused if someone else changed the workflow since you read it. Read it again
and make the change on what is there.

Show the person the graph before a run, or put it in a pull request:

```bash
locks workflow render <name> [--markdown]       # a Mermaid flowchart (fenced, its steps listed)
locks workflow render --run <run> [--markdown]  # a run's copy, each step's state on its node
```

How to write the steps:

- **Prompts:** write each one so a stranger could do it. `{goal}` and any
  other name listed under `inputs` is filled in at run time.
- **What the repository must hold:** a top-level `requires` (a list of paths from the repository's
  top, such as `["docs/tracer"]`) refuses a run, `--dry-run` included, while one of them is absent,
  before any worker is spent.
- **Order:** a step without `needs` waits on the step before it. `needs: []`
  makes a step a first step. Two steps that need the same step run side by
  side when the dispatcher finds them independent.
- **Reading steps:** a review or a plan (`kind` `review-routine`, `plan`,
  `explore`) changes nothing. It reads the work of the steps before it.
- **Writing steps:** each one starts from the commit the nearest writing step
  before it kept.
- **Roles:** `--role <name>` runs a step as one role of the project's team
  (`locks team show`), say `security-auditor` for an audit step. A role the
  project's team does not have is not used: the team picks, and the plan says
  so.
- **Surfaces:** `--surfaces` bounds where a writing step may change files. A
  change outside them fails the attempt and is undone. Steps with disjoint
  surfaces run side by side.
- **Parts:** `--fanout <plan step>` runs a step once per part of that plan
  (a `plan` step before it that it waits on), side by side, each in its own
  surfaces; `--integrate` makes a later step join the parts' branches, with a
  worker only for a conflict or a check that fails on the joined work. A
  review of a large change that touches several areas runs once per area, into
  one ledger.
- **Trunk:** `--trunk <reading step>` starts a step's first try as a fork of
  that step's session (an `explore` or a `plan` step before it that it waits
  on): it starts with what that step read instead of reading it again, and its
  role reaches it as its first message after the fork. A review forks only an
  `explore` step, which decided nothing; refute, recheck and integrate steps
  fork none. Each repository tries forked and fresh first tries against each
  other and keeps forking where it won (`locks stats`). `feature-parallel`'s
  parts fork its plan.
- **Review ledger:** after review steps, `--ledger refute` decides the
  findings only one review saw, `--ledger fix` fixes only the confirmed ones,
  and `--ledger recheck` checks each one. Reviews never vote. A row still open
  gets a second round, then the run waits on the person. `dual-review` and
  `panel` are built this way.
- **Checks:** give a writing step a `verify` command when the repository has
  one. locks runs it itself.
- **Conditions:** `when` maps a step it needs to an outcome that step's ending
  records. The outcomes are `findings` or `clean` (did the work it judged have
  problems the change caused), `changed` or `unchanged` (did it change files),
  or one of a question's options. A step whose `when` does not hold is
  skipped: it passes with no worker, and the steps after it start from the
  work before it. For if/else, give two steps opposite `when`s and let a later
  step need both. The engine decides from locks' records, never from a
  worker's words.
- **Questions:** a step with `ask` (the question, `{inputs}` allowed) and
  `options` (2 to 6 short answers) runs no worker. It waits on the person, and
  the session hears of it as a step waiting on them. Ask the person in your
  conversation, then run their answer with `locks workflow answer <run> <step>
  <option>`. Never answer for them. A question is answered, never resumed.
- **Another workflow as a step:** a step with `workflow: <name>` and `with:
  {input: "text with {goal}"}` runs that saved workflow on the work before it,
  as a run of its own. It passes with the branch that run's last step kept,
  and the run goes on from there. The workflows it calls are checked and
  frozen when the run starts: one that is missing, lacks an input, calls
  itself, or nests deeper than three levels is refused before anything is
  queued. Cancelling the run cancels the run it started.
- **Memory:** a step can carry locks memory into its brief, for considerations
  that step must keep:
  - `--memory note:<id>` (or a bare id) for one project note
  - `topic:<name>` for every live note in a topic
  - `problem:<id>` for a problem with every fix tried on it

  Find the ids with `locks recall "<question>"` or `locks memory search
  "<symptom>"`, and remove them again with `--clear memory`. Notes are read
  only from the project the run is in. A run whose memory cannot be read there
  does not start.
- **No grants, no caps:** a definition cannot hold permission rules,
  approvals or capabilities (those are the person's flags on `run`), and no
  step has a dollar limit: its room is `--max-turns`. A `budget_usd` an older
  definition holds is read and ignored.
- **Cut where context changes hands, never by role:** one step builds with its
  tests (or reproduces and fixes); a review is its own step and reads blind (the
  request and the diff, not what earlier steps said; one with no diff to read,
  such as a plan's critique, gets what came before); fixing what it found goes
  through the ledger (`refute`, `fix`, `recheck`), skipped when the review is
  clean. A step gets the findings of the steps it waits on as records.
- **Changing a bundled template:** it is copied to the user scope first, and
  the template itself stays as it was.

### While it is in use

A workflow that active runs use (in any project, as happens with a global
one in the user scope) is held. A change you make to it is kept as a
**pending revision** and applies by itself when the last run using it ends
or is cancelled. Further changes build on that pending revision.

```bash
locks workflow show <name>        # its version, the runs using it, a pending change
locks workflow pending [<name>]   # what waits, and for which runs
locks workflow pending <name> --apply|--drop
```

Pass `--now` to a change only when the person wants it at once. The runs
keep the copy they started with either way. A held workflow is removed only
with `--force`.

## Run one

Running spends money, so do it only when the person asked for this work.
`locks workflow list` (or `locks workflow run <name> --help`, or `locks
workflow show <name>`) shows what each workflow takes: its inputs, each said
required, and its memory. An input only a step's own check uses (`"verify":
"{check}"`) is filled by the run's `--verify` when it is not named. Check a
run first with `--dry-run`, which queues nothing and says what is missing, so
the first real start is the one that runs:

```bash
locks workflow run <name> --input goal="..." --input check="..." --dry-run
locks workflow run <name> "<value of the one required input not named>" [--verify "<check>"]
locks workflow run <name> --input goal="..." --input area="..." [--verify "<check>"] [--allow RULE] [--allow-approval] [--after <run>]
```

`--verify` (or `--check`, the same flag) is the check of every step that writes and names none of its own;
a reading step never gets it, and a step with no kind gets it once it is
classified into one that writes. A memory topic a step names that holds no
note yet in this project is said when the run starts, with the command that
fills it, and the step goes without it; run that command when the person has
the fact. `--after <run>` holds the new run's first steps until that run
passes, then starts them on the commit its last step kept.

The run starts the repository's dispatcher. Then, without you:

- Each step runs in its own worker, with its own cell, check and escalation.
- The next step starts when that one passes.
- The session that started the run hears of it once: at its end, or when a
  step waits on the person.

## Watch one

```bash
locks workflow status [--all]           # one line per run: the step it is at, its attempt, last commit, files
locks workflow status <run> [--verbose] # a line a step: state, task, cell, attempt, cost, branch, why it stopped
```

The inputs show clipped to one line (`--verbose`, or `--full`, shows them
whole). One run shows a line a step: its state and task, its cell and
attempt, its cost so far, the branch it kept, why it stopped when it did and
how far it is behind the base; then the steps waiting on the orchestrator and
the branch that holds the work so far. `--verbose` adds, under each step, what
a running worker is doing, the commit a done step kept and the files it
touched.
`locks overview` lists the runs of every project. `locks show <task>` shows
one step in depth.

When a step stopped:

- **Read why:** `locks show <task>`.
- **Can go on:** `locks resume <task> --unattended`, or `locks workflow retry
  <run> <step>` to queue it again as a new task. Either way the run goes on by
  itself once the step passes. Leave `--unattended` off only for a resume that
  carries what the person just granted (`--allow`, `--allow-approval`, `--keep`).
- **Should stop:** `locks workflow cancel <run>`. No further step starts.
- **Asks the person:** `locks workflow answer <run> <step> <option>`, with the
  answer they gave in the conversation.
- **Runs another workflow:** that run goes on by its own steps (`locks workflow
  status <its run>`). A step of it that stopped is resumed or retried there,
  and its end passes the step here.

Some things only the person may clear: a refused command, an approval, a
branch to merge. Tell them; do not clear it for them.
