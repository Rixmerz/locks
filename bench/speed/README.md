# Speed benchmarks: where locks' wall time goes, and what shortens it without costing quality

Real workers, real money. Each benchmark measures one thing locks could do faster, beside what it
does today, and keeps the result that decides it.

```bash
python bench/speed/overlap.py <commit> 3            # locks' check beside the verifier, or after it
python bench/speed/build.py run sonnet/high,sonnet/xhigh,opus/high,opus/xhigh 3 4 && python bench/speed/build.py report
python bench/speed/plan.py run opus/high,opus/xhigh,opus/max 2 3 && python bench/speed/plan.py report
python bench/speed/handoff.py run self,locks 4 4 && python bench/speed/handoff.py report
python bench/speed/dispatch.py current,named2 3 && python bench/speed/dispatch.py report
python bench/speed/build.py run sonnet/high,sonnet/high+batch 4 4      # a `+batch` arm asks for parallel calls
python bench/speed/versus.py run locks,opus/low,opus/medium 3 3 [--hard] && python bench/speed/versus.py report
python bench/speed/versus.py run omega,locks 3 3 [--hard]             # X: the "Omega" recipe against locks
```

## Summary: what was measured, built, left alone, and never measured

Every row below is a real run judged by hidden tests or by locks' own records; each links to its
section. Three to four runs a cell is noisy: a difference under about 20% is not read as one.

**Built, because a measurement showed it:**

| Change | Release | Evidence |
|---|---|---|
| locks' check runs beside the independent verifier | 0.45 | A: −42% on the median, the check not slowed |
| features start at sonnet/high (was sonnet/xhigh) | 0.45 | B: as right, 3 to 4 times sooner |
| a plan starts at opus/high (was opus/xhigh) | 0.45 | C: the same parts in half the time |
| the worker does not run the full check before it reports | 0.47 | E: 131 s against 216 s, as right |
| the dispatcher's wait names a collision | 0.47 | F: independent pairs started 42/42 (was 29/42), colliding ones held |
| a start rises only on climbs | 0.48 | G: history had pinned writing kinds a rung or two high |
| the mod hears an ending within about 2 s | 0.48 | H: 9 s p50 and 16 s p90 before |
| notes are filed after the ending | 0.49 | J: 5.3 s p50 off every step |
| a writing kind classified high starts at its ladder's first cell | 0.50 | M: 213 s against 468 s, as right |
| the session passes the kind it read (`--kind`) | 0.50 | L, O: skips a 6 s classifier call |
| the verifier runs after the ending (`late_verifier`), `locks run` exits with it | 0.51 | R: the harder build in 122 s against 203 to 213 s |
| Opus at low effort is no cheap cell | 0.51 | Q: two of three medium builds climbed for nothing |
| a plan classified high is not raised to opus/xhigh | 0.51 | C, and plans in real use: 47% first-try at opus/xhigh, p50 10 min |

**Measured and not built, and why:**

| Idea | Why not |
|---|---|
| the verifier at sonnet/medium or opus/low | K, Q: 20 and 18 of 42 bugs where high catches 27 |
| the verifier at opus/medium | Q: 36 of 42, but 81 s and three times the cost; a candidate, not proven on more changes |
| a turn cap on the verifier | Q: no faster, no fewer bugs missed |
| asking workers for parallel tool calls (`+batch`) | I: slower on both builds |
| starting held work before its predecessor's verifier ends | D: nothing in the templates waits on a verified step |
| starting a review before the build's check ends | D: one review in four thrown away, to save about 65 s of a 15 to 40 minute build |
| Opus for the classifier or the dispatcher | O: one more right in 72, at 20 to 37 times the cost, no faster |
| splitting a build whose halves read each other | P: slower, and the merged tests failed |
| a guidelines CLAUDE.md (`andrej-karpathy-skills`) in locks' workers | N: diffs 4 to 15% smaller, no arm but opus/low faster |
| skipping locks' check after the worker ran it | the overlap hides it wherever a verifier runs; about 4% elsewhere |
| splitting a review with no parts by folder | `bench/review/areas.py`: slower and dearer |
| an Opus boss at xhigh briefing Haiku workers ("Omega") | X: as right, 1.6 to 4.6 times slower and 1.5 to 7 times dearer; Opus spent 96-98% of it |

**Do not change without a new measurement:**
- *The verifier's cell (the work's model at high effort).* Every cheaper cell measured missed a
  third of the bugs; it found blockers in 16 of 53 checks here.
- *Writing kinds' starts.* Raising them for complexity or for passes where they started made
  builds 2 to 5 times slower and no more right (B, G, M).
- *The rule that a dispatcher's wait names a collision.* Its old caution held every
  independent pair back (F).
- *Haiku as the delegator.* Measured against Opus and Sonnet (O).
- *Racing a start that rarely passes its first try.* It judges itself in each repository
  (`locks.racing`); a fixed answer here would undo that.

**Never measured:**
- throughput of several tasks at once across separate repositories, and the capacity level's
  rises and halvings under a real rate limit;
- the late verifier's false alarms and the reopenings it causes in real use, and how often a
  session merges a branch before its late check has spoken;
- the verifier at sonnet/xhigh, or opus/medium on more than one change;
- a build in a real product repository with a browser check (every build here is a small Python
  engine with `make test`);
- builds on a cell other than sonnet/high and opus at low effort in `versus.py` for a whole
  feature-parallel run (plan, parts, integrate, review);
- why `review-critical` and `test` pass under half their runs in real use (see below);
- whether a plan's parts at sonnet/high build as well as opus/high's (C).

**What a month of real use says** (one person's store over 30 days and several releases, 569 runs, 478 tasks):
the worker is 19% of the timed time and the verifier 3%; 52% is pickup (queued work waiting
for a dispatcher) and 23% `unnoticed` (a worker that died with nobody's session open to sweep it;
its longest runs, 60 to 80 hours, span weekends). `locks stats` splits the pickup by cause; the board shows
the same from 0.51. The orchestrating session's own turns cost more than every worker together
($2,195 against $1,227 at API rates). By kind, `feature` passes first 41% of the time and costs
$9.16 per pass, `plan` 47% and $7.88, `review-critical` 25%, `test` 46%. The biggest lever left
is outside the worker: the time work waits before it starts and after it dies.

## Where the time went (this machine's store, before 0.45)

| Step | Minutes in all | What weighs |
|---|---|---|
| build | 472 | its worker (381) |
| fix | 177 | its independent verifier (44), its check (111 s per attempt at the median) |
| review | 111 | its worker |
| recheck | 66 | its worker (now decided by the fix's verifier where it answered every row) |
| refute | 35 | its worker |

Over 53 attempts that the independent verifier checked, locks' check (p50 61 s) and the
verifier (p50 170 s) ran one after the other: 237 minutes. Run side by side, they would have
taken 66 minutes less.

## A: locks' check beside the independent verifier (`overlap.py`)

The setup is a worktree of locks at a real commit (`206ec54`), locks' full suite as the check,
and a real verifier (`verifier_brief`, opus/high, read-only) on that commit's change. Each of
the three reps ran one after the other, as locks did before 0.45, and then side by side, on a
machine with 4 CPUs.

| Rep | One after the other | Side by side | Check alone | Check beside the verifier |
|---|---|---|---|---|
| 1 | 792 s | 260 s | 262 s | 260 s |
| 2 | 822 s | 478 s | 270 s | 263 s |
| 3 | 1,235 s | 533 s | 260 s | 263 s |

The check was not slowed by the verifier beside it. The verifier passed in all six runs and
found the same things (2 to 3 findings). Side by side saved the whole check or more every time:
the median went from 822 s to 478 s (−42%).

locks does this from 0.45: the `overlap` setting, `orchestrate.Ahead`.

## B: which cell gets a build done soonest, and right (`build.py`)

Each run is one worker on a fresh copy of the pricing engine, and hidden tests the worker never
sees judge the result. A build is right only when every hidden test passes. There were three
runs per cell for each build.

**Medium build:** three pricing rules, a receipt and a CSV export (7 hidden tests).

| Cell | Right | Wall p50 | Turns | Cost p50 |
|---|---|---|---|---|
| sonnet/high | 3/3 | 57 s | 6 | $0.14 |
| opus/high | 3/3 | 118 s | 10 | $0.43 |
| sonnet/xhigh | 3/3 | 170 s | 40 | $0.45 |
| opus/xhigh | 3/3 | 192 s | 24 | $0.77 |

**Harder build:** the same feature, plus moving 24 rules into a folder per stage and renaming
their methods everywhere (10 hidden tests).

| Cell | Right | Hidden tests | Wall p50 | Turns | Cost p50 |
|---|---|---|---|---|---|
| sonnet/high | 3/3 | 30/30 | 84 s | 12 | $0.23 |
| sonnet/xhigh | 3/3 | 30/30 | 276 s | 57 | $0.81 |
| opus/high | 2/3 | 29/30 | 189 s | 16 | $0.82 |
| opus/xhigh | 2/3 | 29/30 | 382 s | 49 | $1.72 |

More effort tripled the wall time and passed no more hidden tests. sonnet/xhigh, where every
feature started before 0.45, was right as often as sonnet/high, at 3 to 4 times its time. From
0.45 a feature starts at sonnet/high. A feature classified as high complexity starts a rung up,
and a repository's history and its calibrated standard still raise the start where its features
needed more.

## D and its cousin: starting what waits before its predecessor is verified

**Work held on a step whose verifier is still running.** No verified step in this store had
anything waiting on it in the current templates:
- A build or repair followed by a blind review runs no verifier.
- A fix's re-check is now decided by that same verifier.
- Work held with `--after` on another task happened once.

Not built.

**A review started before the build's own check finished.** The check failed 12 times in 50
after a worker claimed a pass. Starting the review early would save the check's median 65 s on
builds of 15 to 40 minutes, and throw away one review in four. Not built.

## C: how much effort a plan for parts needs (`plan.py`)

The plan step of `feature-parallel` was asked for the build of `build.py`, two runs per cell.
locks' own parts reader (`launch.read_parts`, strict) judged each answer.

| Cell | Valid | Parts | Wall p50 | Cost p50 |
|---|---|---|---|---|
| sonnet/high | 2/2 | 4, 4 | 59 s | $0.18 |
| opus/high | 2/2 | 3, 3 | 93 s | $0.29 |
| opus/xhigh | 2/2 | 3, 3 | 189 s | $0.55 |
| opus/max | 2/2 | 3, 3 | 510 s | $1.56 |

opus/high, opus/xhigh and opus/max drew the same three parts (rules with checkout, receipt,
export). From 0.45 a plan starts at opus/high, where it started at opus/xhigh. sonnet/high split
the work another way (checkout apart from the rules). Whether those parts build as well was not
measured, so it is not the start.

## Where the time is now

Over this store's 1,148 minutes of runs:

| Phase | Minutes | What shortens it |
|---|---|---|
| the workers' own turns | 885 | the start cells: features at sonnet/high, plans at opus/high |
| the independent verifier | 171 | now beside locks' check |
| locks' check | 81 | now behind the verifier, wherever one runs |
| pickups | 30 | the lost wake-up, fixed |
| notes | 8 | |
| the plan | 1.5 | |

What is left is mostly each turn's model latency, which locks does not set. Measured and not
built:
- skipping locks' check when the worker ran the same command last: the overlap already hides it
  wherever a verifier runs, and the rest is about 4% of a build;
- reviews at lower effort: quality lost;
- splitting a review of a change with no parts by its folders: slower and dearer.

## E: the worker's own full check before it reports (`handoff.py`)

Worker transcripts on this machine (28 sessions, 13,123 s) spent 64% of their wall time waiting on
the model and 29% (3,793 s) in tests the workers ran themselves. Many of those runs were the whole
suite, 130 to 180 s each, three or four times in one task. The brief told a worker to run the near
tests as it goes and the full check "yourself once, at the end, before you report". Then locks ran
the same check again.

The pricing engine got a slow suite: 12 more test files of 5 s each, 60 s in all. The worker,
sonnet/high, got locks' own brief in two arms. `self` was the line before 0.47. `locks` said:
"Do not run it yourself before you report: locks runs it as soon as you do, and hands you its
failures to fix." locks' check ran after the worker. A red check would have continued the
worker's session with its output, up to twice. Hidden tests judged each build.

| Build | Arm | Right | Green | Wall p50 | Worker's own full runs |
|---|---|---|---|---|---|
| medium | self | 3/4 | 4/4 | 216 s | 6 |
| medium | locks | 4/4 | 4/4 | 131 s | 0 |
| harder | self | 3/3 | 3/3 | 208 s | 3 |
| harder | locks | 3/3 | 3/3 | 152 s | 1 |

No run in either arm needed a second round, and none was less right without its own full run.
From 0.47 the brief says the `locks` line.

## F: does the dispatcher's judgment hold independent work back (`dispatch.py`)

A queued writing task that is told to wait waits for the whole run of the work ahead, often
10 minutes or more. Haiku decides whether it starts beside that work, and its prompt said "when
unsure, answer false". Asked about clearly separate work (an invoice CSV export beside a profile page),
it waited 8 times in 8. Its reasons were "declares no surfaces" and helpers the two "might share".

Real `dispatch.decide` calls, 3 runs per pair. The pairs come in two sets. The tuning set has 14
independent pairs and 16 that collide (one needs the other's change, or both change the same
code). The held-out set has 6 and 6, written after the prompt was chosen, from real requests.

| Prompt | Set | Independent pairs started | Colliding pairs started |
|---|---|---|---|
| before 0.47 ("when unsure, answer false") | tuning | 29/42 | 0/48 |
| a wait names its collision | tuning | 42/42 | 1/48 |
| the same, and a result resting on behaviour the other changes is a collision | tuning | 42/42 | 0/48 |
| before 0.47 | held out | 14/18 | 0/18 |
| the last one | held out | 18/18 | 0/18 |

From 0.47 the dispatcher asks with the last prompt. A wait it gave is also asked again, at most
every two minutes, once the work ahead changed files it had not changed when the wait was
judged. The first judgment is often made when that work has just started and changed nothing.

## G: history kept raising where a task starts (`build.py`, the store)

A task's start rose to where half of the last tasks of its kind had passed. A task that started
high and passed there counted as a reason to start the next one as high, so a start history had
raised never came down, and the cells below were never tried again. In this machine's store:

| Kind | Passed where they started (last tasks) | Start history gave | Start from climbs only |
|---|---|---|---|
| feature | 11/14 | opus/high | sonnet/high |
| implement | 19/20 | sonnet/high | sonnet/medium |

`build.py` with the cell the fix leaves implement at, three runs per build, beside the cells
measured before:

| Cell | Medium build: right, wall p50 | Harder build: right, wall p50 |
|---|---|---|
| sonnet/medium | 3/3, 48 s | 3/3, 69 s |
| sonnet/high | 3/3, 57 s | 3/3, 84 s |
| opus/high | 3/3, 118 s | 2/3, 189 s |

From 0.48 only a climb raises a writing kind's start. A reading kind keeps the old count: its
pass is its answer, and `bench/review/effort.py` showed a review at lower effort missing the
hardest bug.

## H: an ending reaches the idle session sooner (the mod's pulse)

When work ended while the session was idle, the mod heard of it at its next poll, every 15 s
(`locks endings` and the board: about 1.2 s of two processes each time). In this store the
mod's deliveries took 9 s at the median and 16 s at p90 from the ending. A task's ending now
touches a file beside the store, and the mod stats it every 2 s and polls at once when it
changed: within about 2 s plus the poll, with no process between endings.

## I: asking workers to make independent tool calls in one turn (`build.py`, `+batch`)

Worker transcripts here made 1.18 tool calls per turn, and 64% of a worker's wall time was the
model's own latency. Fewer turns could be faster. The `+batch` arm adds one line to the request:
make the calls that do not depend on each other in the same turn. sonnet/high, four runs per arm:

| Build | Arm | Right | Wall p50 | Turns p50 |
|---|---|---|---|---|
| medium | as briefed | 4/4 | 76 s | 5.5 |
| medium | `+batch` | 4/4 | 80 s | 30 |
| harder | as briefed | 4/4 | 100 s | 11 |
| harder | `+batch` | 4/4 | 111 s | 26.5 |

It was slower in both, so it was not built.

## J: filing notes off the critical path

A passing run filed what its worker learned (a Haiku call) before its `finished` event and before
its workflow advanced: 5.3 s at the median and 7.5 s at p90 over 107 runs in this store, on every
step of every workflow. From 0.49 a run files them after its ending and the advance; a
`notes_filed` event keeps the time it took.

## K: the independent verifier at medium effort (`bench/review`)

The verifier is 12% of this store's run time. At sonnet/medium it was faster (34 s against 58 s on
a small fix, 36 s against 51 s on the large change) but caught 20 of 42 planted bugs on the large
change where sonnet/high caught 27. Quality lost: not built (`bench/review/README.md`).

## L: the classifier's model call

A request no rule claims is classified by Haiku before its plan: 4 to 9 s, nearly all of a small
task's plan (`cProfile` on `orchestrate.plan`: 8.9 of 9.7 s). The rules claimed 94 of the 118
requests in this store and the model 12 (the rest predate the field), so it costs about one task
in ten a few seconds. The rules are deliberately not a keyword list for every request
(`classify.py`), so this is left as it is.

## M: locks against one plain Claude Code session (`versus.py`)

The same request, repository and hidden tests for every arm. `locks` is `locks run … --verify 'make
test'` with a locks home of its own (no history); the others are one `claude -p` at that cell
with the request as its whole prompt. Three runs per arm, four for the smoke rows.

| Build | Arm | Right | Wall p50 | Cost p50 |
|---|---|---|---|---|
| medium | locks 0.49 | 4/4 | 89 s | $0.20 |
| medium | locks 0.50 | 3/3 | 91 s | $0.19 |
| medium | opus/low | 4/4 | 87 s | $0.44 |
| medium | opus/medium | 3/3 | 98 s | $0.52 |
| harder | locks 0.49 | 3/3 | 468 s | $1.43 |
| harder | locks 0.50 | 3/3 | 213 s | $0.54 |
| harder | opus/low | 3/3 | 86 s | $0.59 |
| harder | opus/medium | 3/3 | 213 s | $1.29 |

Every arm was right every time. On the medium build locks was as fast as one Opus session at low
effort and cost half as much. On the harder build locks 0.49 started at sonnet/xhigh, one rung up
for a request classified high, and took five times as long. From 0.50 a writing kind classified
high starts at its ladder's first cell, as `build.py` showed it passing there as often.

The rest of locks' 213 s, read from one run's events: its plan 6 s, the worker 105 s, the
independent verifier 50 s (the change touched a pricing checkout: a high risk), and filing notes
7 s after the ending, which a foreground `locks run` still waits for. The verifier is the one
locks gives that one session does not; the rest is the cell's own pace.

## N: where locks' time goes against one session, and a guidelines CLAUDE.md (`versus.py`)

Pulling locks apart on the harder build (three runs each, every one right):

| Arm | Wall p50 | Cost p50 |
|---|---|---|
| one session, opus/low | 86 s | $0.59 |
| locks, start pinned at opus/low, no independent verifier | 91 s | $0.48 |
| locks, its own start (sonnet/high), no independent verifier | 108 s | $0.27 |
| locks as it runs | 213 s | $0.54 |

locks' own machinery (plan, worktree, check, report) is about 5 s. The cell is about 17 s, at half
the cost. The independent verifier is the rest: the change touches a pricing checkout, a high
risk. Over 53 checks in this store it found blockers in 16, so it stays. A faster cell for it
was measured in `bench/review` (`effort.py`): sonnet/medium caught 20 and opus/low 18 of the 42
bugs sonnet/high's 27 cover, and opus/medium caught 36 in 81 s at three times the cost.

`andrej-karpathy-skills`' CLAUDE.md (think first, simplicity, surgical changes, verifiable goals),
committed in the copy, against the same sessions and locks. All 33 runs were right; lines are what
the work added, at the median:

| Build | Arm | Wall p50 | Cost p50 | Lines added | Of them, tests |
|---|---|---|---|---|---|
| medium | opus/low | 84 s | $0.44 | 181 | 61 |
| medium | opus/low + guidelines | 75 s | $0.43 | 153 | 54 |
| medium | sonnet/high | 78 s | $0.27 | 261 | 125 |
| medium | sonnet/high + guidelines | 101 s | $0.34 | 253 | 144 |
| medium | locks | 85 s | $0.20 | 226 | 104 |
| medium | locks + guidelines | 99 s | $0.19 | 198 | 78 |
| harder | opus/low | 119 s | $0.82 | 341 | 164 |
| harder | opus/low + guidelines | 95 s | $0.69 | 310 | 139 |
| harder | sonnet/high | 152 s | $0.54 | 430 | 231 |
| harder | sonnet/high + guidelines | 156 s | $0.57 | 379 | 195 |
| harder | locks | 192 s | $0.48 | 371 | 185 |
| harder | locks + guidelines | 196 s | $0.49 | 357 | 166 |

The guidelines kept diffs 4 to 15% smaller, most of it in tests, and made opus/low faster; they
made sonnet and locks no faster. Three runs a cell is noisy: nothing here was built from it.

## O: which model makes locks' own small decisions (`delegate.py`)

The classifier and the dispatcher are Haiku calls. The same prompts and schemas, with only the
model changed, two runs each: the classifier on 36 requests labelled with the kinds that fit
them (the rules skipped, so this is the model's part alone), the dispatcher on `dispatch.py`'s 42
pairs.

| Decision | Model | Right | Wall p50 | Cost against Haiku |
|---|---|---|---|---|
| classify | haiku/low | 71/72 | about 6 s | 1× |
| classify | sonnet/low | 70/72 | about 6 s | |
| classify | opus/low | 72/72 | about 6 s | 20× |
| dispatch | haiku/low | 84/84 | | 1× |
| dispatch | opus/low | 84/84 | | 37× |

Opus was right once more in 72 at twenty times the cost and no faster: both stay on Haiku. The
classifier runs only for a request no rule claims, about one in ten here, and the session that
sends work already read the request: from 0.50 the orchestration guide and the mod's `queue`
tool tell it to pass `--kind`, which skips the call. Whether an intermediary pays when the
session itself is the same model as the worker is answered by M and N: a session at opus/low on
its own took 86 to 96 s on the harder build; locks pinned to that cell with no verifier took
91 s. The intermediary costs about 5 s; what it adds is the check, the worktree and the record.

## P: one build as two tasks side by side (`versus.py`, `locks-split`)

The medium build as the two tasks a session would send at once (the pricing rules; the receipt
and the export), each with its kind, merged after. Three runs:

| Arm | Right | Wall p50 | Merge conflict |
|---|---|---|---|
| locks, one task | 3/3 | 88 s | |
| locks-split, two tasks | 7/7 hidden tests | 113 s | none |

Slower, and the repository's own tests failed after the merge, though each branch passed alone
and the hidden tests passed (why was not dug into: the receipt's half reads the discounts the
other half adds). Splitting a build whose halves read each other's change is not worth it; `route.split_first` already asks for a
split only for builds of several independent pieces.

## Q: a start at Opus low effort, with the verifier (`versus.py`, `locks@opus/low`)

| Build | Arm | Wall p50 |
|---|---|---|
| medium | opus/low, one session | 87 s |
| medium | locks pinned at opus/low, with the verifier | 209 s |
| harder | locks pinned at opus/low, no verifier | 91 s |
| harder | locks pinned at opus/low, with the verifier | 259 s |

Opus at low effort counted as a cheap cell, so a medium-risk change it made called the verifier,
and two of the three medium builds climbed to opus/high on what it said, though every arm at
opus/low passed every hidden test on its own. From 0.51 a cheap cell is Haiku or a model below
Opus at low effort.

The verifier's own cell on `bench/review`'s large change (42 planted bugs, `effort.py`):

| Verifier cell | Caught | Wall p50 |
|---|---|---|
| sonnet/high (as it runs) | 27/42 | 51 s |
| sonnet/high, 20 turns | 30/42 | 67 s |
| sonnet/medium | 20/42 | 36 s |
| opus/low | 18/42 | 36 s |
| opus/medium | 36/42 | 81 s, $0.45 |

A turn cap saved nothing. The faster cells missed a third of what high catches. In this store
the verifier found blockers in 16 of 53 checks, at 19 turns p50 and 33 p90: it stays where it is,
and moves off the critical path instead (R).

## R: the independent check after the ending (`versus.py`, 0.51)

From 0.51 a pass that locks' own check settled ends at once, and the verifier reads the kept
branch after it (`late_verifier`). The wall time below ends when `locks run` exits; the late check
is waited for apart. Three runs per build, every one right on the hidden tests:

| Build | locks 0.50 | locks 0.51 | Late check after the ending | Cost p50 |
|---|---|---|---|---|
| harder | 203 to 213 s | 122 s (114 to 136) | 66 to 108 s | $0.47 |
| medium | 88 to 91 s | 82 s (78 to 88) | none: no verifier needed | $0.18 |

On the harder build one late check in three found a blocker in work the hidden tests passed and
reopened it as `review`: a false alarm, answered by a resume that gives the worker the blocker.
Before 0.51 the same blocker earned a correction round before the ending, which costs the same
worker time but held every run's ending for it. Whether reopenings like this are rare or common
in real use was not measured (see the summary's "never measured").

The first measurement of this lost every late check: the detached work was a `fork` of the run,
whose SQLite connection does not survive one, and the child's writes vanished when it closed. It
is a fresh process now; the bench caught what the unit tests, which run it in-process, could not.

## S: a cheaper cell for `refactor` and `test` (`kinds.py`, 0.62)

Real workers, three runs per cell, on fresh copies of the memory bench's pricing engine
(`bench/memory/hard`). `refactor` renames the rule methods and moves each rule module by its
stage: right only when all five checks hold (the new layout, the old names gone, and a set of
carts priced exactly as the untouched engine prices them). `test` writes tests for the money and
line helpers: right only when they pass, no source changed, and they kill all eight mutants. On
the untouched engine the judges give 2/5 and 2/8.

| kind | cell | right | held | wall p50 | turns p50 | $ per right |
|---|---|---|---|---|---|---|
| refactor | haiku/high | 3/3 | 15/15 | 113 s | 24 | 0.020 |
| refactor | sonnet/medium | 3/3 | 15/15 | 21 s | 6 | 0.104 |
| test | haiku/high | 3/3 | 24/24 | 134 s | 25 | 0.030 |
| test | sonnet/medium | 3/3 | 24/24 | 27 s | 8 | 0.110 |

Haiku was as right as the first rung of both ladders, at a fifth of the cost per right answer,
and about five times slower: four times the turns. That is enough to *try* it, not to start
there: `matrix.PROBATION` now holds Haiku below `refactor` and `test` as it did below
`implement`, so each repository tries it in turns with the first rung, and it starts there only
where its first tries pass as often. Three runs per cell are a small sample; the trial, not this
table, decides.

## T: the diagnosis method on an unclear bug (`methods.py`, 0.63)

Real Haiku workers (haiku/high), four runs per arm, on fresh copies of the pricing engine with one
planted bug, half-even rounding where the engine rounds half up, told only by its symptom: "some
order totals come out a cent off, but only sometimes". The arms are the same request with and
without the diagnosis protocol locks briefs `debug-unclear` workers with. A run is right when the
suite passes, the prices are the engine's, and no test was touched.

| arm | right | wall p50 | turns p50 | $ p50 |
|---|---|---|---|---|
| with the diagnosis | 4/4 | 100 s | 22.5 | 0.026 |
| without | 2/4 | 76 s | 27 | 0.020 |

Both misses without it claimed a pass: they changed something and stopped, the cause still in
place. With it every worker reproduced the failure first and fixed the rounding mode. Four runs a
side is a small sample, so the method stays on trial per repository (`methods.arm`); this is why
it starts in its trial instead of being left out until one proves it.

## U: haiku/medium for `refactor` and `test` (`kinds.py`, 0.64)

The same asks and judges as S, three more runs on haiku/medium:

| kind | cell | right | wall p50 | turns p50 | $ per right |
|---|---|---|---|---|---|
| refactor | haiku/high | 3/3 | 113 s | 24 | 0.020 |
| refactor | haiku/medium | 3/3 | 48 s | 14 | 0.017 |
| refactor | sonnet/medium | 3/3 | 21 s | 6 | 0.104 |
| test | haiku/high | 3/3 | 134 s | 25 | 0.030 |
| test | haiku/medium | 3/3 | 92 s | 19 | 0.021 |
| test | sonnet/medium | 3/3 | 27 s | 8 | 0.110 |

haiku/medium was as right as haiku/high, faster and cheaper: it is now the probation cell below
both ladders. It is still two to three and a half times slower than the first rung, so
`standards.PROBATION_SLOWER` decides per repository whether a start there stays.

## V: which cell plans (`plans.py`, 0.64)

Real read-only planners on the harder pricing engine, asked to plan a rule whose four traps are
found only by reading the code (the house rounding helper beside a legacy one, the tier nested under
`loyalty` that may be null, written in capitals, and a rule registered in two places). A plan is
scored by the traps it names; three plans per cell.

| cell | all four named | traps named | wall p50 | turns p50 | $ p50 |
|---|---|---|---|---|---|
| opus/high | 2/3 | 11/12 | 50 s | 19 | 0.465 |
| sonnet/high | 3/3 | 12/12 | 29 s | 5 | 0.077 |
| sonnet/medium | 3/3 | 12/12 | 25 s | 5 | 0.074 |

Sonnet named every trap at a sixth of the cost and half the time. A plan now starts at sonnet/high
(the margin over medium costs nothing here), and a plan classified high starts a rung up, on
opus/high. A plan for parts (`feature-parallel`) keeps opus/high: that is where its own bench
measured it, and sonnet was not measured there.

## W: which cell takes an unclear bug (`methods.py`, 0.90.0)

The same planted bug and judges as T, three runs per cell and arm on sonnet/high and opus/high. The
judge changed first: a worker that fixed the rounding and *added* a regression test was judged wrong
under "no test changed" (every opus/high run had), so a run is right when no test line was taken away
(the planted bug breaks the suite, so a worker still cannot pass by editing a test out).

| cell | arm | right | wall p50 | turns p50 | $ p50 |
|---|---|---|---|---|---|
| sonnet/high | with the diagnosis | 3/3 | 38 s | 9 | 0.086 |
| sonnet/high | without | 2/3 | 30 s | 16 | 0.072 |
| opus/high | with the diagnosis | 3/3 | 69 s | 13 | 0.274 |
| opus/high | without | 2/3 | 44 s | 7 | 0.182 |

With the method sonnet/high was as right as opus/high, in little over half the time and at a third of
the cost; without it each missed once in three. From 0.90.0 `debug-unclear` starts at sonnet/high and
climbs to opus/high; the method stays on trial per repository (`methods.arm`). Three runs a side is a
small sample: the climbs by rung in `locks stats` are the record that judges the start.

## X: the "Omega" orchestrator-and-workers recipe against locks (`versus.py`, `omega`, 0.92.0)

A recipe published for Claude Code: the session runs Opus 5.5 at xhigh effort as the boss, which plans,
writes each worker a brief that ends in a "Done when:" check and reruns that check itself; the workers
are Haiku 5.5 subagents (a `builder` and a `reviewer`, one job each); context compacts at 40%
(`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`); a CLAUDE.md of rules and three docs files are the memory. The arm
writes those files into the copy and runs it as one `claude -p` (locks' hooks off), its Haiku subagents
counted in the CLI's own cost. locks 0.92.0 ran with a locks home of its own, no history. Same request,
same hidden tests, three runs each (Claude Code 2.1.319).

| build | arm | right (hidden) | wall p50 (min-max) | $ p50 | $ of the 3 | +lines p50 | +test lines p50 |
|---|---|---|---|---|---|---|---|
| medium | locks 0.92.0 | 3/3 (7/7 each) | 83 s (77-88) | 0.22 | 0.64 | 209 | 93 |
| medium | omega | 3/3 (7/7 each) | 381 s (373-398) | 1.65 | 4.62 | 440 | 237 |
| hard | locks 0.92.0 | 3/3 (10/10 each) | 442 s (411-473) | 1.45 | 4.40 | 533 | 323 |
| hard | omega | 3/3 (10/10 each) | 700 s (620-719) | 2.23 | 6.45 | 722 | 441 |

Every run of both arms passed every hidden test, and no run's own check passed while the hidden ones
failed. The difference is time and money: on the medium build Omega took 4.6 times as long and cost 7
times as much; on the hard one 1.6 times as long and 1.5 times as much (locks' every hard run started at
sonnet/xhigh and its independent check ran after the ending, read and passed each time, its cost in the
task's). The Haiku workers were not where Omega's money went: they cost $0.03 to $0.09 a run, 2 to 4%; the
Opus boss reading, briefing and rerunning checks spent the rest. The recipe's own sum ("a Haiku token
costs a fortieth of an Opus one") prices the tokens the workers spend, and the boss's are most of them.
Its diffs were about twice as large, mostly tests.

What locks does differently, and what this measures: one worker at the cell its rules start a kind at,
a check locks runs itself, and an independent reader only where the risk asks for one. Nothing here
argues for a Haiku worker under an Opus planner on a build of this size; it was not built. Three runs a
cell, one repository: a difference under about 20% is not read as one, and these are far above it.

## Y: locks 0.95.0 against 0.92.0 on the same build (`versus.py`)

Whether the releases since X changed what a build costs and how long it takes: the same request and
hidden tests, a locks home of its own with no history, Claude Code 2.1.319. The medium build ran six
times (one invocation was meant for the hard build and ran the medium one), the hard build three.

| build | arm | right (hidden) | wall p50 (min-max) | $ p50 | $ mean |
|---|---|---|---|---|---|
| medium | locks 0.92.0 | 3/3 (7/7 each) | 83 s (77-88) | 0.22 | 0.21 |
| medium | locks 0.95.0 | 5/6 (one 6/7) | 65 s (44-74) | 0.21 | 0.20 |
| hard | locks 0.92.0 | 3/3 (10/10 each) | 442 s (411-473) | 1.45 | 1.47 |
| hard | locks 0.95.0 | 3/3 (10/10 each) | 366 s (340-416) | 1.17 | 1.24 |

Wall time fell 22% on the medium build and 17% on the hard one, cost 20% on the hard one and not at
all on the medium one. Each is at the edge of what three runs a cell can tell (a difference under about
20% is not read as one), and nothing here says which change did it. One medium run of 0.95.0 passed its
own check and missed one hidden test, where no earlier run of locks had: one in six is not a rate, and
it is the number to watch at the next measure. One medium run started at sonnet/medium instead of
sonnet/high and was the fastest and cheapest of the six, every hidden test passed.

## Z: livespec on the hard build: the rung it raised, and the dossier (`versus.py`, 0.96.0)

Three runs a row of the hard build, `locks run` with its late check, on one machine. "Handed" is 0.95.0
(section Y): the worker given livespec's tools. "Dossier" is locks asking the index itself and briefing
the answers (3,535 characters, 8 symbols for this request), the worker handed nothing. "Off" is
`LOCKS_LIVESPEC=off`.

| livespec | start | all hidden tests right | wall p50 (range) | $ p50 |
|---|---|---|---|---|
| handed, start raised (0.95.0) | sonnet/xhigh | 3/3 | 366 s (340-416) | 1.17 |
| dossier, start raised | sonnet/xhigh | 3/3 | 361 s (237-393) | 1.20 |
| off | sonnet/medium once, sonnet/high twice | 3/3 | 83 s (49-91) | 0.43 |
| dossier, start not raised | sonnet/high | 3/3 | 87 s (80-91) | 0.47 |

What cost the time was never the tools nor the dossier: with an index, the plan started this build one
rung up because a symbol the request names "has 21 callers", and sonnet/xhigh took four times as long as
sonnet/high for the same ten hidden tests. The index now raises a reading kind's start only. At the same
start the dossier neither helped nor hurt here: 87 s against 83 s and $0.47 against $0.43 are inside what
three runs can tell, and every run passed every hidden test either way. This build is small enough to
read whole; whether the dossier pays on a repository too large for that is not measured here. The
"21 callers" is itself doubtful: the dossier lists one caller for the same method, so the count looks
like every `apply_line` of every rule class added together.

## AA: the `engineering-baseline` skill in the writing roles (`versus.py`, 0.96.0)

Three runs a row, both arms interleaved and three runs at once (so every time is longer than a run
alone, section Z), `locks run` with its late check. "With" is 0.96.0 as released; "without" is the
`-nobaseline` arm of that release, the writing roles shadowed without the skill (the default since, the
`-baseline` arm puts it back).

| build | baseline skill | system prompt | all hidden tests right | wall p50 (range) | $ p50 | lines added p50 |
|---|---|---|---|---|---|---|
| medium | with | 29,286 chars | 3/3 | 86 s (72-94) | 0.20 | 216 |
| medium | without | 12,271 chars | 3/3 | 64 s (60-68) | 0.16 | 173 |
| hard | with | 29,286 chars | 3/3 | 131 s (109-133) | 0.51 | 355 |
| hard | without | 12,271 chars | 3/3 | 80 s (78-99) | 0.51 | 317 |

Without the skill the same tests passed 26% sooner on the medium build and 39% sooner on the hard one,
the two arms' ranges not touching on either; cost fell 16% on the medium build and not at all on the
hard one, and the workers wrote fewer lines. What this does not measure: the hidden tests judge behaviour,
not what the skill asks for beyond it (naming, error handling, security habits), so a loss there would
not show here. The reading roles and the verifier keep the skill: they were not measured.

## AB: the dossier on a repository too large to read whole (0.96.0, observed)

Three read-only questions about locks' own code (who calls a function, where an event is written and
read), each asked once with livespec on and once off, haiku/medium, no verifier. One run a cell: an
observation, not a rate.

| question | livespec | wall | turns | tool calls | input tokens | $ |
|---|---|---|---|---|---|---|
| callers of `verdict_card` | dossier (2,432 chars) | 29 s | 7 | 5 | 1.14M | 0.037 |
| | off | 38 s | 12 | 10 | 1.75M | 0.053 |
| callers of `at_base` | dossier (3,012 chars) | 54 s | 15 | 12 | 2.08M | 0.096 |
| | off | 57 s | 14 | 12 | 2.72M | 0.100 |
| where `claim_released` is written and read | dossier (2,187 chars) | 24 s | 9 | 7 | 1.12M | 0.019 |
| | off | 22 s | 7 | 6 | 1.10M | 0.017 |

Added up, the briefed runs read 22% fewer input tokens (4.34M against 5.56M) and cost 11% less; the gain
is on the two questions about a symbol's callers, which is what the dossier holds, and nothing on the
question about an event name, which it does not. The briefed run of the third question left no report
block.

## AC: one worker against the `feature` and `feature-parallel` workflows (`flow.py`, 0.97.0)

The hard build of `versus.py`, the same request and hidden tests, each run in its own locks home, four
runs at once in the first batch and two in the second. "Ended" is the run reaching its last step.

| arm | runs | ended | all hidden tests right | wall | $ |
|---|---|---|---|---|---|
| `single` (`locks run`) | 2 | 2 | 2/2 | 67 s, 75 s | 0.39, 0.43 |
| `feature` (build, review, refute, fix, recheck) | 2 | 1 (one stopped at its refuter, for the person) | 2/2 | 151 s, 259 s | 0.52, 0.85 |
| `feature-parallel` (plan, parts, integrate, review chain) | 6 | 1 (one more stopped at its second re-check) | 2/6 | 844 s and 937 s where it built; 99 to 518 s where it stopped | 5.29 and 6.29; 0.51 to 7.56 |

One worker passed every hidden test in a quarter to a half of the `feature` chain's time, and the chain
found nothing the hidden tests needed: its build step alone had already passed them. The review opened
2 and 5 rows; whether those rows were worth 80 to 190 s is not judged here, since the hidden tests do
not read what a review reads. `feature-parallel` is not for a build this small, and it showed a defect
besides: in six of six runs the plan's first answers were refused (`inconclusive`, twice, then a pass at
opus/xhigh in four runs; stopped in two), 480 to 570 s and $4 to $5 on the plan alone. Forked against
fresh first tries (`LOCKS_TRUNK_ARM`) cannot be told apart on these rows: one run each got as far as its
build, 100 s forked and 94 s fresh.

The plan's refusals had one cause, read from a kept store: `surfaces.disjoint` called
`tests/test_rule_*.py` and `tests/test_receipt.py` overlapping, since both sit under `tests/`, so a
correct plan was sent back twice and then climbed. With surfaces in one folder read apart segment by
segment, two more runs planned on their first answer (224 s and 227 s, $0.89 and $0.90, where it had
been 480 to 570 s and $4 to $5). Both then stopped at their first part: sonnet/high, then the next
model, then "the approach was wrong", 186 s and 204 s, the other parts never started. Why that part
fails is not established; `feature-parallel` on this build has yet to end clean.

## AD: `feature-parallel` ends once a rename is read whole (`flow.py`, 0.99.1)

The first part of `feature-parallel` on the harder variant stopped as "the approach was wrong" in six runs of
six. Its store, kept with `LOCKS_BENCH_KEEP`, showed why: the part moved 30 rule files with `git mv`, and each
old name of `git status -z` was read with three characters cut (`/tally/rules/auto.py`), outside its surfaces;
the work, which passed 67 tests, was undone twice. One run after the fix, same request and hidden tests:

| arm | wall | cost | hidden | parts | first tries passed |
|---|---|---|---|---|---|
| parallel-fresh, before | 414 s (stopped) | $1.89 | 2 of 10 | 2 | 0 of 1 |
| parallel-fresh, 0.99.1 | 529 s | $2.31 | 10 of 10 | 3 | 3 of 3 |

One worker on the same request is still 67 to 75 s and $0.39 to $0.43 (AC): the plan alone took 219 s.
