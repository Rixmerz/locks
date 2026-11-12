# What each ending means and what to do with it; unattended resumes; paused lanes

## The endings notice's rules

A session hears these in full in its first endings notice of a day, and the notices after it say
only that the rules are here. The text is the same as the notice's:

- Act on each ending now: tell the person what happened.
- Every stop is yours to carry. One only the person can grant (a refused command's rule, a cell's
  approval) you ask them for, and on their yes you run its line. Any other you decide as its line
  says: resume it, rewrite it, or dismiss it with why (`locks dismiss N --why '<why>'`).
- Nothing waits on the person unasked. Never redo a worker's task by hand.
- A kept branch is not their checkout: it is merged only on their word.
- A pass says its verdict in one line: the check locks ran, the criteria met, the independent check.
  Criteria not met, and anything not checked, are said in their own words. The record line says the
  verdict is locks' own, so the code is not read again to confirm it; open the diff only where it
  says something was not checked or not met, or when the person asks.
- The worker's answer is cut in a notice; `locks show N` has it whole. A reading task's report is
  its deliverable and is kept whole on the task too.

**Read the report and act on its status.** The summary is the worker's own
account; `changed (from git)` is what the files say. Never tell the user a
file was created or changed unless that line names it, and remember that a
branch is not their checkout: nothing is there until it is merged.
- `done` with a branch — tell the user the summary and its verdict (the
  check that passed, each criterion with its evidence); merge only when they
  say so. Show the diff only if they ask for it. On their word,
  `locks merge N M …` merges kept branches into the branch checked out (the
  ones that touch no other first, the check after each; a conflict stops it
  and names the task that reconciles it), and `locks pr N` opens a draft pull
  request with the verdict as its description.
- `replan` with `split` — it outgrew its turns and is several pieces. Its work
  is kept. `locks split N` shows the pieces; `locks split N --queue` starts
  them on that work, and what waited for N waits for the last piece. Resume it
  instead only when it is truly one piece (it gets more turns).
- `needs_approval` — the next cell needs a person (Fable). Ask; on a yes, the
  person reruns it with `--allow-approval`.
- `replan` — the task as written cannot be done, or does not fit. Rewrite or
  split it from the report's reason, then dispatch again. Do not rerun it as is.
- `blocked` — a refused command or the environment stopped it (an older
  task may say a dollar budget stopped it: budgets are gone, and
  `locks resume <id> --unattended` continues it as it is). `stopped by the
  network` is a drop that outlasted locks' own waits (30s, 90s, 270s):
  its work is kept, nothing is wrong with it; resume it yourself with
  `--unattended` once the network answers.
  The report's `stopped by …` line says which, and the line under it is the
  command that continues it. Tell the user both, in those words. Its work
  so far is on its branch, marked unverified. When the user grants what was
  refused, give them that command to run (`locks resume <id> --allow "<the refused rule>"`):
  the person runs it, never you.
  It is not a new task, and you never redo it by hand. When the person
  wants the rule for every task here, add `--keep`: one approval per
  program, not one per task.
- `failed` — every cell on the ladder tried. Report what each attempt found
  (`locks show <id>`); do not try it yourself in this context.
- `blocked` by `review` — the independent check's blockers stayed open on
  work that passed its own check: read them (`locks show <id>`), then resume
  it for one more round with `--unattended`, or dismiss it with why
  (`locks dismiss <id> --why '<why>'`) when the blockers are wrong.
- `blocked` by `decision` (exit 4) — the work is done: it changed files, its
  check passed, and it is kept on its branch; the worker names one point that
  needs a design decision, quoted in the reason. It did not fail. Tell the
  person what was done and the point; on their answer, merge the branch and
  send what the decision asks as a new task, or dismiss it with why.

- `replan` because it outgrew its turns — the task is not wrong. locks
  resumes it once on its own with more turns (as it does a worker that died,
  a network that stayed down, or a machine under load), so you hear of it only
  when that resume ends; if it stops again, continue it with
  `locks resume <id> --max-turns 120 --unattended`, or split what is left.

A resume you start on your own, with nothing the person just granted,
carries `--unattended`: it refuses what only they can clear (a refused
rule, a cell that needs their approval) and stops after two runs' worth of
attempts. A resume that carries what they granted in this conversation
(`--allow`, `--allow-approval`, `--keep`) is theirs to run, not yours: give
them the line, to type in a terminal or after `!` in this prompt. Claude
Code's permission check refuses it from an agent as a change to its own
permissions.

A task that stops pauses its lane, and the queued work behind it waits.
When its ending says the lane is paused, deal with the stop first, then
tell the person which queued tasks wait; when they do not depend on the
stopped one, run `locks lanes --unpause <repo>` so they start now, and say
you did. Never leave queued work waiting for the person to press play.

A queued task that "waits for #N" is the dispatcher's call that it may touch
the work ahead. The call is made again once that work ends; when you know it
is independent of what runs now (other files, another area), run
`locks lanes --beside N` and it starts beside it. A task held `--after` other
work waits for it to pass: that is its order, and `--beside` leaves it.

An attempt that changed outside its surfaces: read its summary and check output (`locks show N`) before its label; the check may not have run at all.
