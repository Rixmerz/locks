"""After a failed attempt: which way to move, if any.

A failure is evidence about *where* the problem lives, and each place has one
right response. Conflating them is how an orchestrator burns attempts and money going in
circles:

- **Retry** — the work never happened (a timeout, a missing binary). Same cell,
  once. Escalating it spends the top tier on a problem no model would solve.
- **More effort** — the work happened and was shallow: a missed edge case, a
  bug left behind, no verification. Same model, the next effort on the ladder.
- **Next model** — the work happened and was *wrong*: the approach was off, or
  two cells already gave the same answer. More thinking from the same model is a
  bet that is already settled; the next model enters at its own cell.
- **More turns** — the worker hit its turn ceiling. A ceiling is not a fault and
  does not clear on its own; it is raised once, then the task is split.
- **Replan** — the spec or the architecture is wrong. No model fixes a task that
  should not exist; the run stops and says so.
- **Decide** — the work is done (it changed files and the check that shows it
  passed), but the worker names one point that needs a design decision. That is
  no failure and no task to rewrite: the work is kept on its branch and the
  orchestrator decides the point.
- **Review** — the work passed its own check and nothing is left to climb
  to; only the independent check's blockers stand. The work is kept for a
  person, never dropped as a failure.
- **Blocked** — a person has to act: the environment failed twice, or the
  worker was refused a command (a refusal is a setting, so retrying it, or
  sending a stronger model into it, gets the same refusal).

Deliberately absent from the inputs: the model's stated confidence. A model's
opinion of its own output is not evidence about the output.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from locks import findings as findings_mod
from locks.matrix import EFFORT_PURPOSE, MODELS, Cell
from locks.text import few, reads_alike


class Failure(StrEnum):
    CODE_BUG = "code_bug"  # attempted, left a bug or an edge case
    TEST_BUG = "test_bug"  # attempted, the test it relied on was wrong
    INCONCLUSIVE = "inconclusive"  # no verdict, or a pass it could not show
    APPROACH = "approach"  # attempted, misunderstood or went the wrong way
    SPEC_BUG = "spec_bug"  # the task as written cannot be done
    ARCHITECTURE_BUG = "architecture_bug"
    ENVIRONMENT = "environment"  # never attempted: timeout, missing tool, transport
    PERMISSION = "permission"  # a tool call the worker needed was refused by the permission settings
    TURNS_EXHAUSTED = "turns_exhausted"


#: What each failure says about where the problem lives, as a move explains itself.
MEANING = {
    Failure.CODE_BUG: "the work happened and left a bug or a missed edge case",
    Failure.TEST_BUG: "the work happened, but the test it relied on was wrong",
    Failure.INCONCLUSIVE: "no verdict, or a pass without evidence",
    Failure.APPROACH: "the work went the wrong way: the task was misunderstood",
    Failure.SPEC_BUG: "the task as written cannot be done",
    Failure.ARCHITECTURE_BUG: "the design the task assumes is wrong",
    Failure.ENVIRONMENT: "the work never ran: a timeout, a missing tool, a transport error",
    Failure.PERMISSION: "a tool call the worker needed was refused by the permission settings",
    Failure.TURNS_EXHAUSTED: "the worker used all its turns before a verdict",
}

EFFORT_AXIS = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.INCONCLUSIVE})
REPLAN = frozenset({Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG})


class Move(StrEnum):
    RETRY = "retry"
    MORE_EFFORT = "more_effort"
    NEXT_MODEL = "next_model"
    MORE_TURNS = "more_turns"
    REPLAN = "replan"
    NEEDS_APPROVAL = "needs_approval"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    CONVERGED = "converged"
    REVIEW = "review"
    DECIDE = "decide"


#: The moves after which another attempt runs.
CONTINUES = frozenset({Move.RETRY, Move.MORE_EFFORT, Move.NEXT_MODEL, Move.MORE_TURNS})
#: Which dial a move turns; every other move stops the run.
AXIS = {Move.RETRY: "retry", Move.MORE_EFFORT: "effort", Move.NEXT_MODEL: "model", Move.MORE_TURNS: "turns"}

MAX_ENV_RETRIES = 1
#: Tries after the network dropped, and the wait before each: a network comes
#: back, and the same cell, waited for, goes on from the work it kept.
NETWORK_WAITS = (30, 90, 270)
#: Tries after a check's tests timed out on a loaded machine, and the wait before each: the
#: dispatcher's level halves on it (`locks.capacity`), and the same cell goes again.
LOAD_WAITS = (60, 180)
MAX_TURNS_CEILING = 200
#: Times one cell is asked again for an answer only its form failed, before it climbs.
RESHAPES = 1


@dataclass(frozen=True)
class Attempt:
    cell: Cell
    max_turns: int
    passed: bool
    failure: Failure | None = None
    summary: str = ""
    #: The tool calls the permission settings refused, as `Bash(npm run build)`.
    denied: tuple[str, ...] = ()
    #: The `--allow` rules that would let them through, one per program.
    allow: tuple[str, ...] = ()
    #: What this attempt itself changed: work that moved, even when it ran out.
    changed: tuple[str, ...] = ()
    #: The output that failed it, for the next attempt's brief.
    evidence: str = ""
    #: What locks' own check said when it failed on it (`launch.check_tail`): shown first in the
    #: next brief, since a red check is the actual failure the next worker starts from.
    check_tail: str = ""
    #: The launch broke, not the work (see `WorkerResult.transient`).
    transient: bool = False
    #: The problems it reported, as records: two cells that place the same ones agree.
    findings: tuple[dict[str, str], ...] = ()
    #: What says the network dropped under it (see `WorkerResult.network`).
    network: str = ""
    #: The check's tests timed out on a loaded machine (see `WorkerResult.load`).
    load: str = ""
    #: The failing lines of locks' check that turned it down (see `WorkerResult.failing`).
    failing: frozenset[str] = frozenset()
    #: A race's other try stopped because this task's passed first (`orchestrate._raced_attempt`,
    #: `RACED_OUT`): it found nothing, so it is no failure of the work, and a pass beside it is no
    #: pass after a failure. One that failed its own check before the other passed is a failure like any.
    raced: bool = False
    #: Why that failure could not be told from the base's (see `WorkerResult.base_unknown`).
    base_unknown: str = ""
    #: The API turned its session away (`usage`: the account's own limit), and the seconds
    #: until it said to try again, or until that limit resets (see `WorkerResult.limited`).
    limited: str = ""
    retry_after_s: float | None = None
    #: The independent check's blockers, when they are all that failed it: the
    #: work passed its own check (or had none to run and its own check passed).
    blockers: tuple[dict[str, str], ...] = ()
    #: Only its answer's form failed (see `WorkerResult.shape`): what made it unreadable.
    shape: str = ""
    #: The work stands though the worker reported a failure: it changed files and the
    #: check that shows them passed (locks', or with none given the worker's own).
    stands: bool = False
    #: The test files outside the task's surfaces that are the only place locks' check fails
    #: (`surfaces.failing_outside`): a climb cannot fix what its surfaces keep it from.
    outside: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    move: Move
    cell: Cell | None = None
    max_turns: int = 0
    reason: str = ""
    #: The chain of evidence behind the move, in order: what the attempt ended
    #: with, what that means, the rule it was read against, where that leads.
    because: tuple[str, ...] = ()
    #: Cells of the ladder the move went past, each with why.
    skipped: tuple[str, ...] = ()
    #: Seconds to wait before the next attempt (a network that dropped).
    wait_s: int = 0
    #: The same cell answers again in its own session: only the form of its answer failed.
    reshape: bool = False
    #: It outgrew its turns as several pieces: split, not given more (`locks split`).
    split: bool = False
    #: It stopped because two cells left the same failing tests: a third would repeat them.
    stuck: bool = False
    #: The standard change whose skip it followed (`standards.calibrate_skips`), if any.
    standard: int | None = None

    @property
    def continues(self) -> bool:
        return self.move in CONTINUES

    @property
    def axis(self) -> str:
        return AXIS.get(self.move, "stop")


def network_streak(attempts: Sequence[Attempt]) -> int:
    """How many attempts in a row, the last ones, the network dropped under."""
    n = 0
    for a in reversed(attempts):
        if a.passed or not a.network:
            break
        n += 1
    return n


def load_streak(attempts: Sequence[Attempt]) -> int:
    """How many attempts in a row, the last ones, the check's tests timed out under load."""
    n = 0
    for a in reversed(attempts):
        if a.passed or not a.load:
            break
        n += 1
    return n


def waited(a: Attempt) -> bool:
    """A try that said nothing about the work: the network dropped, or the machine's load
    timed its tests out. It counts toward no attempt ceiling and no repeated answer."""
    return bool(a.network or a.load)


def repeated_answer(attempts: Sequence[Attempt]) -> bool:
    """The last two failures, at different cells, said the same thing.

    Same model and effort agreeing with itself is determinism, not a finding,
    so only a pair from different cells counts.
    """
    return _repeated_pair(attempts) is not None


def reshape_streak(attempts: Sequence[Attempt]) -> int:
    """How many attempts in a row, ending with the last, failed only the form of their answer on its cell.

    A plan held back for its reader's findings (`locks.critique.SHAPE`) passed in form: it is no
    failure of the answer's form and counts for nothing, so its one return never becomes a climb."""
    streak, cell = 0, attempts[-1].cell
    for a in reversed(attempts):
        if a.passed or not a.shape or a.cell != cell:
            break
        if not a.shape.startswith("critique"):
            streak += 1
    return streak


def _repeated_pair(attempts: Sequence[Attempt]) -> tuple[int, int] | None:
    """The 1-based numbers of the two failures that said the same thing."""
    # A try the network dropped under said nothing about the work.
    failed = [(i, a) for i, a in enumerate(attempts, 1) if not a.passed and not waited(a)]
    if len(failed) < 2:
        return None
    (i, a), (j, b) = failed[-2], failed[-1]
    if a.cell == b.cell or a.failure != b.failure:
        return None
    if a.findings and b.findings:
        # Findings as records: the same places named is the same answer, whatever the words.
        same = findings_mod.places(a.findings) == findings_mod.places(b.findings) != frozenset()
        return (i, j) if same else None
    return (i, j) if reads_alike(a.summary, b.summary) else None


def _position(ladder: Sequence[Cell], cell: Cell) -> int:
    try:
        return ladder.index(cell)
    except ValueError:
        # A pinned or learned cell outside this ladder: place it where its model
        # and effort would sort, so the next move still climbs.
        key = cell.sort_key()
        return sum(1 for c in ladder if c.sort_key() < key) - 1


def _next_effort(ladder: Sequence[Cell], cell: Cell) -> Cell | None:
    for candidate in ladder[_position(ladder, cell) + 1 :]:
        if candidate.model == cell.model:
            return candidate
        return None
    return None


def _next_model(ladder: Sequence[Cell], cell: Cell) -> Cell | None:
    for candidate in ladder:
        if candidate.model_rank() > cell.model_rank():
            return candidate
    return None


def _skipped(ladder: Sequence[Cell], cell: Cell, target: Cell) -> tuple[str, ...]:
    """The cells between where the work was and where the move goes, all of the
    same model: more effort from a model whose approach was the problem."""
    if target not in ladder:
        return ()
    between = ladder[_position(ladder, cell) + 1: ladder.index(target)]
    return tuple(f"{c.label}: more effort from {c.model}, which is not what failed" for c in between
                 if c.model == cell.model)


def _correction(last: Attempt, why: str) -> str:
    """What every move taken for a correction says: the work passed its own check, only the independent
    check's blockers failed it, and `why` this rung."""
    return (f"the work passed its own check; the independent check found {len(last.blockers)} blocker(s) in it, "
            f"so this is a correction: {why}")


def _arrives(target: Cell) -> str:
    purpose = f" ({EFFORT_PURPOSE[target.effort]})" if target.effort else ""
    return f"next: {target.label}{purpose}"


def _unknown_base(last: Attempt, seen: str) -> Decision | None:
    """A claimed pass locks' check turned down, whose failure the same check at the base could
    not be compared with (it timed out or could not run there): nothing shows the work caused it.
    Not charged to the work and not climbed: the task stops, its work kept, for a resume."""
    if last.failure is not Failure.ENVIRONMENT or not last.base_unknown:
        return None
    return Decision(
        Move.BLOCKED, reason=f"base unknown: {last.base_unknown}; its work is kept: resume it, and the base's run "
                             "is tried again",
        because=(seen, "locks' check failed and the same check at the base could not say what fails there: "
                       "nothing shows the work caused it, so it is not charged to the work",
                 "no stronger cell is a better judge of a base that did not answer: a climb re-runs the whole check "
                 "for the same unknown",
                 "its work stays on its branch; a resume compares again"))


def _same_failing(attempts: Sequence[Attempt], seen: str) -> Decision | None:
    """The last attempt left exactly the failing tests an earlier one left, on another try: the
    climb between them changed nothing that fails, and a second one would repeat it."""
    last = attempts[-1]
    if not last.failing or (last.failure or Failure.INCONCLUSIVE) not in EFFORT_AXIS:
        return None
    earlier = next((a for a in attempts[:-1] if not a.passed and not waited(a) and a.failing == last.failing), None)
    if earlier is None:
        return None
    return Decision(
        Move.BLOCKED, stuck=True,
        reason=f"the check failed the same {len(last.failing)} failing test(s) on {earlier.cell.label} and "
               f"{last.cell.label} ({few(sorted(last.failing))}); a third cell would repeat them: its work is "
               "kept, resume it once someone has looked at them",
        because=(seen, f"{earlier.cell.label} and {last.cell.label} left exactly the same failing tests",
                 "the climb between them was already tried on these tests: a second climb would repeat it, and "
                 "each one re-runs the whole check",
                 "its work stays on its branch"))


#: What `cli.main` says when a worker tries to start locks work; read back from an attempt.
WORKER_GUARD = "it does not start locks work"


def _met_worker_guard(last: Attempt) -> bool:
    """Whether the attempt's own evidence or answer shows locks' worker guard refused it."""
    return WORKER_GUARD in last.evidence or WORKER_GUARD in last.summary


def decide(
    ladder: Sequence[Cell],
    attempts: Sequence[Attempt],
    *,
    allow_approval: bool = False,
    reading: bool = False,
    several: str = "",
    skips: Mapping[tuple[str, str], Skip] | None = None,
) -> Decision:
    """The move after the last attempt, with the evidence it rests on. `reading`
    is a task that only finds out: its answer is what it is for. `several` says why the
    plan sized it as several workers' work: out of turns, it is split, never given more.
    `skips` are the standards' skips here: a climb they name goes past its rung."""
    decision = _decide(ladder, attempts, allow_approval=allow_approval, reading=reading, several=several)
    return _skipping(ladder, decision, attempts[-1].cell, skips or {}, allow_approval)


def _decide(
    ladder: Sequence[Cell],
    attempts: Sequence[Attempt],
    *,
    allow_approval: bool,
    reading: bool,
    several: str,
) -> Decision:
    if not attempts:
        raise ValueError("decide() needs at least one attempt")
    last = attempts[-1]
    if last.passed:
        raise ValueError("the last attempt passed; there is nothing to decide")
    failure = last.failure or Failure.INCONCLUSIVE
    seen = f"attempt {len(attempts)} at {last.cell.label} ended {failure}: {MEANING[failure]}"

    if failure in REPLAN and last.stands and not last.denied:
        # The worker did the work and its check passed, and it names a point the work
        # cannot settle by itself (a design decision). Nothing here is wrong: the work
        # is kept, and the point goes to the orchestrator, quoted.
        point = " ".join(last.summary.split())[:300] or MEANING[failure]
        return Decision(Move.DECIDE, reason=f"the work is done and kept on its branch; one point needs a "
                                            f"decision: \"{point}\"",
                        because=(seen, f"it changed {len(last.changed)} file(s) and the check that shows them "
                                       "passed: the work stands, so this is no wrong task and no failure",
                                 "a decision is no climb: no model makes it for the person, so the work is "
                                 "kept and the point goes to the orchestrator"))

    if failure in REPLAN:
        return Decision(Move.REPLAN, reason=f"{failure}: the task, not the work, is wrong",
                        because=(seen, "no model fixes a task that should not exist: it is rewritten or split, "
                                       "not climbed"))

    if failure is Failure.PERMISSION or last.denied:
        # A refusal is the setting that stopped the work, whatever else the
        # attempt ended with: a check that went red because the worker could not
        # run its tools is not shallow work, and more effort meets the same refusal.
        more = len(last.denied) - 3
        refused = (", ".join(last.denied[:3]) + (f" and {more} more" if more > 0 else "")) or "a tool call"
        grant = " ".join(f"--allow '{rule}'" for rule in last.allow)
        how = f"allow it ({grant})" if grant else "allow it (`--allow`)"
        return Decision(Move.BLOCKED, reason=f"the worker was refused {refused}; {how}, then resume",
                        because=(seen, f"refused: {', '.join(last.denied) or 'a tool call'}",
                                 "a refusal is a setting: a retry, or a stronger model, gets the same refusal",
                                 *([f"the rules that let it through, one per program: {', '.join(last.allow)}"]
                                   if last.allow else [])))

    if _met_worker_guard(last):
        # The task asks its worker to start locks work, which no worker may (`cli.main`'s guard):
        # more effort or another model meets the same refusal. Whatever it did is kept, and what
        # it could not start is the orchestrator's to run.
        return Decision(Move.DECIDE, reason="the task needs locks work started (a run, a workflow, the queue), "
                                            "which a worker never starts: its work is kept; run that part from "
                                            "the session",
                        because=(seen, f"its worker was refused by the guard: \"{WORKER_GUARD}\"",
                                 "a climb meets the same guard: a worker of any cell is refused it"))

    if (unknown := _unknown_base(last, seen)) is not None:
        return unknown

    if last.outside and failure is Failure.CODE_BUG:
        # The check fails only in tests the task's surfaces leave out, and the base does not fail
        # them: more effort or another model meets the same wall (and its continued worker only
        # answers a spec_bug naming that test). The work is kept and the surfaces are the decision.
        return Decision(
            Move.DECIDE,
            reason=f"the check fails only in tests outside its surfaces ({few(list(last.outside))}); its work is "
                   "kept: widen the surfaces to include them, or leave them",
            because=(seen, f"every failing test of locks' check is in a file outside the task's surfaces "
                           f"({few(list(last.outside))}), and the base does not fail them",
                     "a climb cannot change a file its surfaces keep it from: it repeats the same failure",
                     "the work stays on its branch; a resume with those files as surfaces goes on from it"))

    if failure is Failure.ENVIRONMENT and last.network:
        # The network dropped under the work: no fault of the work, the cell or the
        # environment a person keeps, and it comes back. The same cell, after a wait,
        # goes on from what the worktree holds; only a network that stays down stops it.
        dropped = network_streak(attempts)
        kept = (f"it changed {len(last.changed)} file(s) first: they stay in the worktree and the next try goes "
                "on from them" if last.changed else "nothing was changed: the next try starts it again")
        if dropped <= len(NETWORK_WAITS):
            wait = NETWORK_WAITS[dropped - 1]
            return Decision(Move.RETRY, last.cell, last.max_turns,
                            f"the network dropped ({last.network}); the same cell again in {wait}s",
                            because=(seen, f"the network failed under it ({last.network}): a network comes back, "
                                           "and no stronger cell is faster without one", kept,
                                     f"try {dropped} of {len(NETWORK_WAITS)} after a drop, in {wait}s"),
                            wait_s=wait)
        return Decision(Move.BLOCKED, reason=f"the network stayed down ({last.network}) through {dropped} tries; "
                                             "resume it once it is back",
                        because=(seen, f"it dropped {dropped} times in a row, waited for each time", kept))

    if failure is Failure.ENVIRONMENT and last.load:
        # The worker's own run passed; locks' check timed its tests out on a machine
        # busier than its CPUs. No fault of the work: the level halves on it, and the same
        # cell, after a wait, checks again from what the worktree holds.
        streak = load_streak(attempts)
        if streak <= len(LOAD_WAITS):
            wait = LOAD_WAITS[streak - 1]
            return Decision(Move.RETRY, last.cell, last.max_turns,
                            f"the check's tests timed out under load ({last.load}); the same cell again in {wait}s",
                            because=(seen, f"most of what failed were tests out of time ({last.load}) after the "
                                           "worker's own run passed: a loaded machine, not a bug, and no stronger "
                                           "cell makes the machine faster",
                                     "the machine's level halves on it, so fewer workers share it",
                                     f"try {streak} of {len(LOAD_WAITS)} after a timeout under load, in {wait}s"),
                            wait_s=wait)
        return Decision(Move.BLOCKED, reason=f"the check's tests kept timing out under load ({last.load}) through "
                                             f"{streak} tries; its work is kept: resume it when the machine is quieter",
                        because=(seen, f"they timed out {streak} times in a row, waited for each time",
                                 "the work stays on its branch"))

    if failure is Failure.ENVIRONMENT and last.limited == "usage":
        # The account's own limit: every cell meets it until it resets, so nothing is tried
        # before then; the work, whatever it got to, waits on its branch for the reset.
        return Decision(Move.BLOCKED, reason="the account's usage limit stopped it; its work is kept, and it resumes "
                                             "once the limit resets",
                        because=(seen, "the account's limit turns every session away until it resets: a retry, "
                                       "more effort or another model meets the same limit",
                                 "its work stays on its branch; the resume after the reset goes on from it"))

    if failure is Failure.ENVIRONMENT:
        retries = sum(1 for a in attempts if a.failure is Failure.ENVIRONMENT)
        if last.changed:
            # The work ran: it changed files before the environment stopped it.
            # A retry is for work that never ran; the same cell in the same
            # environment meets the same wall, so a person looks at it now.
            return Decision(Move.BLOCKED, reason="the environment stopped work that had started; fix it, then resume",
                            because=(seen, f"it changed {len(last.changed)} file(s) first: the work ran, so this is "
                                           "no launch that broke, and the same environment stops a retry the same way",
                                     "its work is kept; a resume goes on from it"))
        if not last.transient:
            # The worker ran, looked, and named the wall (an old Node, a server it
            # cannot start), or locks' own check could not run: the same cell
            # meets it again at once. Of 18 such retries, none passed.
            return Decision(Move.BLOCKED, reason="the worker found the environment in the way; fix it, then resume",
                            because=(seen, "the worker ran and named what stops it: no launch broke, so a retry "
                                           "in the same environment meets the same wall",
                                     "what it found is in its account; a resume goes on once it is fixed"))
        if retries <= MAX_ENV_RETRIES:
            return Decision(Move.RETRY, last.cell, last.max_turns, "the work never ran; same cell",
                            because=(seen, "a retry is not an escalation: nothing was tried, so nothing says a "
                                           "stronger cell would do better; the same cell, once",
                                     f"next: {last.cell.label} again"))
        return Decision(Move.BLOCKED, reason="the environment failed twice; fix it, then resume",
                        because=(seen, f"it failed to run {retries} times: past the one retry, a person has to "
                                       "look at the environment"))

    if failure is Failure.TURNS_EXHAUSTED and several:
        # Room for one piece is turns; room for several is more workers. Its work is kept,
        # and `locks split` proposes the pieces and starts them on it.
        return Decision(Move.REPLAN, reason=f"it outgrew {last.max_turns} turns as several pieces ({several}): "
                                            "split what is left into tasks that each fit, on the work it kept",
                        because=(seen, f"the plan sized it as several workers' work ({several})",
                                 "more turns on one worker is the climb that costs the most: each turn re-reads "
                                 "everything the work has read so far, so pieces cost less than one long attempt"),
                        split=True)

    if failure is Failure.TURNS_EXHAUSTED:
        raised = sum(1 for a in attempts if a.failure is Failure.TURNS_EXHAUSTED)
        moving = raised > 1 and bool(last.changed)
        if (raised <= 1 or moving) and last.max_turns < MAX_TURNS_CEILING:
            turns = min(last.max_turns * 2, MAX_TURNS_CEILING)
            rule = (f"a turn ceiling is not a fault of the model or its effort: the same cell gets {turns} turns "
                    f"instead of {last.max_turns}" + (", once" if not moving else "")
                    + f" (the cap is {MAX_TURNS_CEILING})")
            return Decision(Move.MORE_TURNS, last.cell, turns, f"hit {last.max_turns} turns; raised to {turns}",
                            because=(seen, *([f"the raised attempt still moved the work: it changed "
                                              f"{len(last.changed)} file(s), so it is raised again"]
                                             if moving else []), rule))
        if last.max_turns >= MAX_TURNS_CEILING:
            why = f"{last.max_turns} turns is the cap"
        else:
            why = "the turns were raised and the raised attempt changed nothing"
        return Decision(Move.REPLAN, reason="the task does not fit even the raised turn ceiling; split it",
                        because=(seen, f"{why}: the task is bigger than one worker, so it is split, not climbed"))

    if failure is Failure.INCONCLUSIVE and last.shape and reshape_streak(attempts) <= RESHAPES:
        # The answer could not be read: no single result block, a plan whose parts cannot
        # run. That is the form, not the depth of the work: a stronger cell pays for a
        # format. The same cell is asked again, in its own session when one is kept, once;
        # an answer it still cannot shape climbs as any inconclusive does.
        return Decision(Move.RETRY, last.cell, last.max_turns,
                        f"its answer could not be read ({last.shape}); the same cell answers again",
                        because=(seen, f"only the answer's form failed: {last.shape}",
                                 "a wrong form is no shallow work: more effort or another model pays for a "
                                 "format; the same cell is told what was wrong and answers again, once",
                                 f"next: {last.cell.label} again"),
                        reshape=True)

    pair = _repeated_pair(attempts)
    if pair and reading:
        # Two cells that only read came back with the same findings: that is the
        # answer, confirmed, not an approach to replace. A stronger model would
        # read the same code and say it a third time, at a higher price.
        first, second = attempts[pair[0] - 1], attempts[pair[1] - 1]
        return Decision(Move.CONVERGED, reason="two cells found the same thing: that is the answer",
                        because=(seen, f"attempts {pair[0]} ({first.cell.label}) and {pair[1]} "
                                       f"({second.cell.label}) of a task that only reads reported the same "
                                       "findings: the answer is confirmed, and climbing would repeat it"))
    if (stuck := _same_failing(attempts, seen)) is not None:
        return stuck
    if pair or failure is Failure.APPROACH:
        if failure is Failure.APPROACH:
            why = "the approach was wrong"
            rule = ("the worker went the wrong way: more thinking from the same model is the bet that already "
                    "lost, so a different model starts over")
        else:
            why = "two cells gave the same answer"
            first, second = attempts[pair[0] - 1], attempts[pair[1] - 1]
            rule = (f"attempts {pair[0]} ({first.cell.label}) and {pair[1]} ({second.cell.label}) failed the "
                    f"same way ({failure}) and their summaries read alike: two cells agreeing is a settled bet, "
                    "so more effort would give the same answer")
        target = _next_model(ladder, last.cell)
        if target is None and last.blockers:
            return _review(last, seen, f"{why}, and there is no stronger model on this ladder")
        if target is None:
            return Decision(Move.REPLAN, reason=f"{why}, and there is no stronger model on this ladder",
                            because=(seen, rule, f"no model above {last.cell.model} on this ladder: the task "
                                                 "is rewritten instead"))
        because = (seen, *([_correction(last, why)] if last.blockers else []), rule, _arrives(target))
        return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval, because,
                      _skipped(ladder, last.cell, target))

    prior = attempts[-2] if len(attempts) >= 2 else None
    if last.blockers and prior is not None and prior.blockers and prior.cell.model == last.cell.model:
        # Two corrections in a row on one model, each leaving blockers the check shows: more
        # of its thinking keeps missing defects of this work, so a stronger model reads it.
        target = _next_model(ladder, last.cell)
        if target is not None:
            why = f"{last.cell.model} left blockers in two corrections in a row"
            return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval,
                          (seen, _correction(last, f"{why}: more effort from it is the bet that already missed "
                                                   "twice, so a stronger model takes the work"), _arrives(target)),
                          _skipped(ladder, last.cell, target))

    # Effort axis: the work was shallow. The next effort of the same model,
    # and only when this model has run out does the next one start.
    shallow = "the work happened and was shallow: the same model, more thorough"
    target = _next_effort(ladder, last.cell)
    if target is not None:
        if last.blockers:
            shallow = _correction(last, "a defect the work missed is the shallow kind: the same model, more thorough")
        return Decision(Move.MORE_EFFORT, target, last.max_turns, f"{failure}: same model, more thorough",
                        because=(seen, shallow, _arrives(target)))
    target = _next_model(ladder, last.cell)
    if target is None and last.blockers:
        return _review(last, seen, f"{last.cell.label} is the top of this ladder")
    if target is None:
        return Decision(Move.EXHAUSTED, reason="the top of the ladder failed too",
                        because=(seen, f"{last.cell.label} is the top of this ladder: there is nothing left "
                                       "to climb to"))
    why = f"{failure}, and {last.cell.label} was this model's last cell"
    if last.blockers:
        shallow = _correction(last, f"{last.cell.label} is {last.cell.model}'s last cell, so a missed defect it "
                                    "could not find goes to a stronger model")
    return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval,
                  (seen, shallow, f"{last.cell.label} was {last.cell.model}'s last cell on this ladder, so the "
                                  "next model takes over", _arrives(target)))


def review_reason(last: Attempt, why: str) -> str:
    """Why work that passed its own check stops for a person: said once, for the ladder and the attempt limit."""
    return (f"{why} and the independent check's {len(last.blockers)} blocker(s) are still open; the work passed "
            "its own check and is kept, unverified, for a person")


def _review(last: Attempt, seen: str, why: str) -> Decision:
    """The ladder is out, and what failed the last attempt is only the independent check's blockers.

    A correction does climb the ladder, as before: a blocker the check shows in work
    that passed is a missed defect, the shallow kind (more effort), and a cheap cell
    that cannot find it hands over to a stronger model. What a correction is not is
    evidence that the work is wrong, so the ladder running out is no `exhausted`: the
    work passed its own check, and dropping it is what the correction cap
    (`orchestrate.MAX_CORRECTIONS`) exists to avoid. It stops as a review, kept."""
    return Decision(Move.REVIEW, reason=review_reason(last, why),
                    because=(seen, "the work passed its own check; only the independent check's blockers failed it, "
                                   "so this is no wrong approach and no failure of the task",
                             f"{why}: nothing is left to climb to", "the work stays on its branch, unverified"))


def _gated(move: Move, cell: Cell, turns: int, why: str, allow_approval: bool,
           because: tuple[str, ...] = (), skipped: tuple[str, ...] = ()) -> Decision:
    if cell.needs_approval and not allow_approval:
        return Decision(Move.NEEDS_APPROVAL, cell, turns, f"{why}; {cell.label} needs your approval",
                        because=(*because, f"{MODELS[cell.model].alias} runs only when a person says so: "
                                           "resume with --allow-approval, or tag the task #fable"),
                        skipped=skipped)
    return Decision(move, cell, turns, why, because=because, skipped=skipped)


@dataclass(frozen=True)
class Skip:
    """A standard's skip (`standards.active_skips`): a climb into a rung goes to `onto` instead."""

    onto: Cell
    change: int
    because: str = ""


def _skipping(ladder: Sequence[Cell], decision: Decision, source: Cell,
              skips: Mapping[tuple[str, str], Skip], allow_approval: bool) -> Decision:
    """The climb `decision` made, past its rung when a standard skips it: on to the rung after,
    as more effort when that rung is the same model's, else as a new model; more effort is never
    skipped onto another model (effort and model are different dials). Only a climb, never
    onto a rung that needs an approval or off the ladder."""
    if decision.move not in (Move.MORE_EFFORT, Move.NEXT_MODEL) or decision.cell is None:
        return decision
    skip = skips.get((source.label, decision.cell.label))
    if skip is None or skip.onto not in ladder or skip.onto.needs_approval or decision.cell.needs_approval:
        return decision
    if decision.move is Move.MORE_EFFORT and skip.onto.model != source.model:
        # Effort and model are different dials: shallow work climbs effort, and a skip never turns
        # it into another model. The climb stays as it was.
        return decision
    move = Move.MORE_EFFORT if skip.onto.model == source.model else Move.NEXT_MODEL
    said = (f"standard change #{skip.change} skips {decision.cell.label} on climbs from {source.label}"
            + (f": {skip.because}" if skip.because else ""))
    because = (*decision.because[:-1], said, _arrives(skip.onto))
    skipped = (*decision.skipped, f"{decision.cell.label}: skipped by standard change #{skip.change}",
               *_skipped(ladder, decision.cell, skip.onto))
    reason = f"{decision.reason}; {decision.cell.label} skipped by standard change #{skip.change}"
    return replace(_gated(move, skip.onto, decision.max_turns, reason, allow_approval, because, skipped),
                   standard=skip.change)
