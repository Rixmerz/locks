"""Model x effort: the two dimensions a task is routed on.

The **model** decides how well the work *notices* things — that a green test
pins a bug, that the spec is wrong, that the obvious fix is the one that
failed last week. The **effort** decides how *thorough* the same model is:
how much it thinks, how many tools it calls, whether it checks itself before
it says it is done. They are different dials, and a router that treats them as
one ladder spends the top tier on work that only needed a second look.

Every effort level has a purpose. None of them is a rung that exists only to
be climbed past:

  ==========  ===================================================================
  ``low``     Direct answers, few tool calls, no preamble. Chat, explanations,
              commit messages, trivial edits. It drops self-checks, which is fine
              where there is nothing to check and wrong anywhere else.
  ``medium``  Saving where quality holds. The starting point for agentic coding on
              Sonnet: implementing from a clear plan, tests, refactors, docs.
  ``high``    The floor for work that needs judgment: debugging with a repro, UI,
              integration, routine review. Usually the best balance.
  ``xhigh``   Most agentic coding and long-horizon work: a feature that crosses
              several parts, a bug with no clear cause, a hard plan.
  ``max``     Correctness over cost. Only where the level below showed headroom,
              or the change is critical.
  ==========  ===================================================================

Haiku has the dial since Haiku 5.5; a cell written without one (``haiku``, as
older runs recorded it) is Haiku at ``medium``, the model's own default. Fable is
reserved for work a person asked for.
"""
from __future__ import annotations

from dataclasses import dataclass

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Each effort's purpose in a few words, as a climb explains where it went.
EFFORT_PURPOSE: dict[str, str] = {
    "low": "direct answers, few tool calls, no self-checks",
    "medium": "saving where quality holds: work from a clear plan",
    "high": "the floor for work that needs judgment",
    "xhigh": "long-horizon work: several parts, no clear cause",
    "max": "correctness over cost",
}


@dataclass(frozen=True)
class Model:
    alias: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    has_effort: bool
    #: A model nothing routes to without a person saying so.
    needs_approval: bool = False
    #: The effort a cell written without one runs at, where the model's dial is
    #: newer than the cells locks already recorded for it.
    default_effort: str | None = None


#: Cheapest first. Prices are first-party API rates per million tokens; on a
#: subscription they are a proxy for how much of the plan a call uses.
MODELS: dict[str, Model] = {
    # Haiku 5.5: a tenth of Haiku 4.5's rates, with the effort dial (default medium).
    "haiku": Model("haiku", 0.10, 0.50, has_effort=True, default_effort="medium"),
    "sonnet": Model("sonnet", 2.0, 10.0, has_effort=True),
    "opus": Model("opus", 4.0, 20.0, has_effort=True),
    "fable": Model("fable", 10.0, 50.0, has_effort=True, needs_approval=True),
}

MODEL_ORDER: tuple[str, ...] = tuple(MODELS)


@dataclass(frozen=True)
class Cell:
    """One point of the matrix: a model, and an effort where it has one."""

    model: str
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.model not in MODELS:
            raise ValueError(f"unknown model {self.model!r}")
        if MODELS[self.model].has_effort:
            if self.effort is None and MODELS[self.model].default_effort:
                object.__setattr__(self, "effort", MODELS[self.model].default_effort)
            if self.effort not in EFFORTS:
                raise ValueError(f"{self.model} needs an effort, one of {EFFORTS}")
        elif self.effort is not None:
            # Passing `--effort` to a model without the dial reaches nothing, and
            # the run would report an effort that never applied.
            raise ValueError(f"{self.model} has no effort dial")

    @property
    def label(self) -> str:
        return self.model if self.effort is None else f"{self.model}/{self.effort}"

    @property
    def needs_approval(self) -> bool:
        return MODELS[self.model].needs_approval

    def model_rank(self) -> int:
        return MODEL_ORDER.index(self.model)

    def sort_key(self) -> tuple[int, int]:
        """Cheaper model first, then lower effort. Not alphabetical: `fable` < `haiku`
        as strings, and a ladder sorted that way would start at the top."""
        return (self.model_rank(), EFFORTS.index(self.effort) if self.effort else -1)

    @classmethod
    def parse(cls, text: str) -> Cell:
        model, _, effort = text.strip().partition("/")
        return cls(model, effort or None)


def ladder(*labels: str) -> tuple[Cell, ...]:
    return tuple(Cell.parse(label) for label in labels)


#: The kinds of work the router knows, each with the cells it climbs, cheapest
#: start first. A ladder is *where escalation may go*, not a path every task
#: walks: the failure decides whether the next attempt moves along effort or
#: jumps to the next model (see ``escalate``).
LADDERS: dict[str, tuple[Cell, ...]] = {
    "chat": ladder("sonnet/low", "sonnet/medium"),
    "classify": ladder("haiku/low", "haiku/medium", "sonnet/low"),
    "explore": ladder("haiku/medium", "haiku/high", "sonnet/medium"),
    "docs": ladder("haiku/medium", "haiku/high", "sonnet/medium"),
    "test": ladder("sonnet/medium", "sonnet/high", "opus/high"),
    "refactor": ladder("sonnet/medium", "sonnet/high", "opus/high"),
    "implement": ladder("sonnet/medium", "sonnet/high", "sonnet/xhigh", "opus/high"),
    "ui": ladder("sonnet/high", "sonnet/xhigh", "opus/high"),
    # A feature starts at sonnet/high: on bench/speed's builds (a medium one and one with a 24-rule
    # refactor), it passed every hidden test 6 times in 6, 3 to 4 times sooner than sonnet/xhigh,
    # which passed them no better. High complexity starts a rung up, and history raises the start.
    "feature": ladder("sonnet/high", "sonnet/xhigh", "opus/high", "opus/xhigh"),
    "debug-repro": ladder("sonnet/high", "sonnet/xhigh", "opus/high", "opus/xhigh"),
    "debug-unclear": ladder("opus/high", "opus/xhigh", "opus/max"),
    # A routine review starts at sonnet/high: reading a change for its defects is the independent check's
    # work, and on `bench/review`'s change (42 planted bugs, `effort.py`) sonnet/medium caught 20 of them
    # where sonnet/high caught 27, 15 s apart. The rung after it is another model, as a reading kind's
    # repeated answer converges (`escalate.decide`), never a third reading by the same one.
    "review-routine": ladder("sonnet/high", "opus/high"),
    "review-critical": ladder("opus/high", "opus/xhigh", "opus/max"),
    # A plan starts at sonnet/high: on bench/speed/plans.py's plan of a rule whose traps are found only
    # by reading the code, sonnet/high and sonnet/medium named all four in 3 plans of 3, at a sixth of
    # opus/high's cost and half its time (opus/high missed one once). A high complexity starts a rung
    # up, on opus/high, and a plan for parts keeps opus/high (`feature-parallel`'s step): on its bench
    # it drew the same valid parts as opus/xhigh and opus/max, and sonnet was not measured there.
    "plan": ladder("sonnet/high", "opus/high", "opus/xhigh", "opus/max", "fable/high"),
    "frontier": ladder("fable/high", "fable/xhigh", "fable/max"),
}

#: What a task nothing recognised runs on: the middle of the matrix, with room
#: to climb both ways it can fail.
DEFAULT_KIND = "implement"

#: The turns a first attempt starts with. A cap is room, not a brake: of 110
#: attempts measured, 20 ran out of turns and 15 of those passed on the next
#: attempt, each paying a fresh worker's orientation again. Passing attempts of
#: every kind but a feature needed under 41 turns (p90 40), so 80 covers them
#: with room; an attempt that is done stops on its own.
DEFAULT_TURNS = 80
#: A feature crosses several parts of the code; UI and test work run, look and run
#: again. A feature's passing attempts needed 59 turns at the median, 104 at p90
#: and 121 at most: 60 cut seven of them short.
TURNS = {"feature": 120, "ui": 120, "test": 120}


def turns_for(kind: str) -> int:
    return TURNS.get(kind, DEFAULT_TURNS)

KINDS: tuple[str, ...] = tuple(LADDERS)

#: Kinds whose work is reading, not writing. Their workers get no edit tools,
#: and they never need an isolated checkout.
READ_ONLY_KINDS = frozenset({"chat", "classify", "explore", "review-routine", "review-critical", "plan"})


def ladder_for(kind: str) -> tuple[Cell, ...]:
    return LADDERS.get(kind, LADDERS[DEFAULT_KIND])


#: A cheaper cell below a writing kind's ladder, tried only on trial (`trust.probation_arm`) where
#: the ladder's first rung already passes its first tries here, and a start only by the standards'
#: own rule (`standards.calibrate`). On bench/memory's builds Haiku passed every hidden test of the
#: readable tasks cold, and of the one that turned on a policy no file states once it was briefed as
#: a constraint, at a sixth of Sonnet's cost. On bench/speed/kinds.py it was as right as the first
#: rung of `refactor` and `test` (every layout check and price kept; every mutant killed, 3 of 3
#: each) at a fifth of the cost per right answer, though about five times the wall time. No other
#: kind was measured, so no other has one.
PROBATION: dict[str, Cell] = {"implement": Cell("haiku", "high"),
                               # haiku/medium was as right as haiku/high on both, and two to one and a
                               # half times as fast (`kinds.py`, 0.64): still slower than the first rung.
                               "refactor": Cell("haiku", "medium"), "test": Cell("haiku", "medium")}


def reach(kind: str) -> tuple[Cell, ...]:
    """The rungs a standard may start a kind on: its probation cell, when it has one, then its ladder."""
    below = PROBATION.get(kind)
    return ((below,) if below is not None else ()) + ladder_for(kind)


def estimate_usd(cell: Cell, input_tokens: int, output_tokens: int) -> float:
    model = MODELS[cell.model]
    return (
        input_tokens * model.input_usd_per_mtok + output_tokens * model.output_usd_per_mtok
    ) / 1_000_000
