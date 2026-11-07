"""Where a task of each size starts, from what was measured: a standard per
repository, kind and points band (`locks.points`), and the record of how each one
moved and why.

The model is the one MECHA uses for people — standard hours per ticket type, the
real hours, and a calibration that moves the standards — here for workers. A
standard holds the start cell and the start turns, and what the finished tasks of
its band measured over the last `WINDOW` of them: how often each cell passed its
first try, what a point cost and how long it took. A calibration runs after every
`CALIBRATE_EVERY` endings in a repository (`settle`, from the hooks or the
dispatcher, never from a GET) and applies two rules, and nothing else:

- the start cell is the cheapest cell of the kind's ladder whose first tries pass
  at least `PASS_FIRST` of the time, over at least `MIN_SAMPLES` of them; it moves
  one rung per calibration, down as far as the ladder's first rung and never below
  it, and never onto a cell that needs an approval;
- the start turns are the 75th percentile of the turns the band's passing tasks
  needed, capped at `escalate.MAX_TURNS_CEILING`, one `TURNS_STEP` per calibration,
  never under the kind's own turns (a cap is room; a lower one saves nothing).

A band moves one field at a time, and not again before that change is judged: once
`JUDGE_AFTER` tasks ran under it, the window before and the window after are
compared (pass-first rate, attempts, time and cost per point), and a change that
made pass-first worse by more than `REVERT_MARGIN` is reverted, one step back, as a
change of its own. Every change and verdict is appended to the store with its rule,
its evidence and locks' version. A calibration may also skip a rung of a kind's ladder
whose climbs rarely pass their next try (`calibrate_skips`), judged by those climbs and
reverted the same way (`_judge_skip`). No model decides any of it, and money moves
nothing but one thing: a start the calibration moved onto a kind's probation cell, below its
ladder (`matrix.PROBATION`, tried on trial by `trust.probation_arm`), is reverted when a point
that passed costs more on it than on the rung it replaced (`PROBATION_COSTLIER`): a cheaper cell
that climbs is cheaper only in name; and so when a point takes more than `PROBATION_SLOWER` times as
long there. Moving onto it is the start-cell rule's alone.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any

from locks import __version__, config
from locks.escalate import MAX_TURNS_CEILING
from locks.matrix import PROBATION, Cell, ladder_for, reach, turns_for
from locks.store import Store

#: The finished tasks of a band its measure reads, newest last.
WINDOW = 20
#: Endings in a repository between two calibrations of its standards.
CALIBRATE_EVERY = 5
#: How often a cell's first tries must pass for it to be a start, and over how many.
PASS_FIRST = 0.7
MIN_SAMPLES = 5
#: The percentile of the turns passes needed that the start turns aim at, and how far
#: one calibration moves them.
TURNS_PERCENTILE = 0.75
TURNS_STEP = 10
#: Tasks run under a change before it is judged, and how much worse pass-first may get
#: before it is reverted.
JUDGE_AFTER = 5
REVERT_MARGIN = 0.2
#: How much more a passed point may cost on a probation start than on the rung it replaced before
#: the start is reverted: nothing more. The cheaper cell is there to cost less for the same work.
PROBATION_COSTLIER = 1.0
#: How many times as long a passed point may take on a probation start as on the rung it replaced
#: before the start is reverted: on bench/speed/kinds.py Haiku was as right as the first rung at a
#: fifth of the cost and about five times the wall time, and a cell that much slower is no saving.
PROBATION_SLOWER = 2.0
#: Failures that say nothing of the work: an attempt that ended only so is no first try.
NO_VERDICT = frozenset({"environment", "permission"})
#: The moves after which an attempt goes on with the same cell's try: room, or a launch that broke.
GOES_ON = frozenset({"retry", "more_turns"})
FIELDS = ("start_cell", "start_turns", "skip")
#: The field of a change that skips a rung of a kind's ladder on climbs from the rung below it
#: (`calibrate_skips`): its value is `from>to`, the climb it replaces. It is kept at `ANY_SIZE`:
#: the ladder is one per kind, never one per size.
SKIP = "skip"
#: The moves a skip may replace: the two climbs of the ladder. A retry or more turns stays on its
#: cell and a network or load wait is a retry: none of them is a climb, and none is ever skipped.
SKIP_MOVES = frozenset({"more_effort", "next_model"})
#: A climb into a rung is skipped once its next try passed under `SKIP_PASS_BELOW` over at least
#: `SKIP_MIN_MOVES` of them. The rungs that asked for it passed 0 of 2 and 0 of 1 again and again;
#: four climbs with at most one pass in five is a rung that costs a whole attempt and almost never
#: pays it back. Far under `PASS_FIRST` on purpose: a start must be good to be chosen, a rung must
#: be nearly useless to be passed over.
SKIP_MIN_MOVES = 4
SKIP_PASS_BELOW = 0.25
#: The climbs of one kind and pair of rungs a skip reads, newest last.
SKIP_WINDOW = WINDOW
#: How much longer, at the median, the passing tasks whose climbs went past the rung may take to
#: pass than those that climbed into it, before the skip is reverted: a skip is for time, and one
#: that does not save it is undone, as one whose climbs pass less by `REVERT_MARGIN` is.
SKIP_SLOWER = 1.25
#: The band of a kind at every size: its start cell is measured over all of the kind's tasks
#: here, for the sizes whose own band has too few to tell (`for_plan`). Its turns never move:
#: what a task needs in turns depends on its size.
ANY_SIZE = 0
#: The last ending a settle read, and each repository's endings since its last calibration.
SEEN_KEY = "standards:seen"
#: Set once every task that ended before it had points was sized and every repository with
#: history calibrated: the standards start from the work already done, not from zero.
BACKFILL_KEY = "standards:backfilled"
#: How a size given after the fact says so.
AFTER_THE_FACT = "sized after it ended, from its text"


@dataclass(frozen=True)
class Ended:
    """A task that ended, as the standards read it."""

    id: int
    repo: str | None
    kind: str
    band: int | None
    status: str
    cost_usd: float
    wall_s: float
    pinned: bool
    attempts: tuple[dict, ...]
    #: The changes whose values its plan started with.
    used: tuple[int, ...]
    ended_at: str
    realized: int | None
    #: Its first try passed with no independent check read: the sampling let it through
    #: (`verifier_sampled`). It is a first try like any, but moves no start cell cheaper.
    unread: bool = False

    @property
    def passed(self) -> bool:
        return self.status == "done"

    @property
    def first(self) -> bool | None:
        """Whether its first attempt passed; None when that attempt said nothing of the work."""
        if not self.attempts:
            return None
        a = self.attempts[0]
        if not a["passed"] and a.get("failure") in NO_VERDICT:
            return None
        return bool(a["passed"])


def ended(store: Store, *, repo: str | None = None, since: str = "", until: str | None = None) -> list[Ended]:
    """The tasks that ended, oldest first: the person's work, an investigator's reading aside."""
    rows = [r for r in store.ended_tasks(repo=repo, since=since, until=until)
            if '"investigation"' not in (r.get("options") or "")]
    attempts = store.attempts_of([r["id"] for r in rows])
    # The tasks whose first try passed unread: the sampling let it through (`trust.sampling`).
    unread = {int(e["task_id"]) for e in store.verifier_record(repo)
              if e["event"] == "verifier_sampled" and int((e["data"] or {}).get("seq") or 0) == 1}
    out = []
    for r in rows:
        used = ((r["planned_data"] or {}).get("standard") or {}).get("changes") or ()
        out.append(Ended(int(r["id"]), r["repo"], r["kind"] or "", r.get("points"), r["status"],
                         float(r["cost_usd"] or 0), float(r["wall_s"] or 0), bool(r["pinned"]),
                         tuple(attempts.get(int(r["id"]), ())), tuple(int(c) for c in used), r["ended_at"],
                         r["ended_data"].get("realized"), int(r["id"]) in unread))
    return out


def _label(text: str | None) -> str | None:
    try:
        return Cell.parse(text).label if text else None
    except ValueError:
        return None


def samples(attempts: Sequence[Mapping[str, Any]]) -> list[tuple[str, bool]]:
    """Each first try of a cell in a task's attempts, and whether it passed. A first try is
    the task's first attempt, or the first after a new model, which starts from the base;
    more effort continues the same session, so its cell has no first try of its own. A try
    goes on through the attempts on its cell after a retry or more turns (room, or a launch
    that broke, never the cell's fault) and passes when one of them did. A try that ended
    only on the environment or a refusal says nothing of the work: it is no sample."""
    out: list[list[Any]] = []
    current: list[Any] | None = None
    before = None
    for i, a in enumerate(attempts):
        if a.get("move") == "raced_beside":
            # The rung's try of a race neither won: a first try of its own, from the base beside
            # the start's, which goes on after it (its try and the move it took stay current).
            said = bool(a["passed"]) or a.get("failure") not in NO_VERDICT
            out.append([_label(a.get("cell")), bool(a["passed"]), said])
            continue
        if a.get("move") == "raced_out":
            # Stopped because the other try of its race passed first: it says nothing of its
            # cell, and the try after it started from the base beside it.
            before = "race"
            continue
        cell = _label(a.get("cell"))
        # A race's rung starts from the base beside the start: a first try of its own.
        if i == 0 or before in ("next_model", "race"):
            current = [cell, False, False]
            out.append(current)
        elif current is None or cell != current[0] or before not in GOES_ON:
            current = None
        if current is not None:
            current[1] = current[1] or bool(a["passed"])
            current[2] = current[2] or bool(a["passed"]) or a.get("failure") not in NO_VERDICT
        before = a.get("move")
    return [(cell, passed) for cell, passed, said in out if said and cell]


def turns_needed(attempts: Sequence[Mapping[str, Any]]) -> int | None:
    """The turns a passing task needed: its passing attempt's and those of the attempts right
    before it on the same cell that ran out of turns. None for no pass, or none recorded."""
    found = [i for i, a in enumerate(attempts) if a["passed"]]
    if not found:
        return None
    i = found[-1]
    total = int(attempts[i].get("turns") or 0)
    j = i - 1
    while j >= 0 and attempts[j].get("cell") == attempts[i].get("cell") and \
            attempts[j].get("failure") == "turns_exhausted":
        total += int(attempts[j].get("turns") or 0)
        j -= 1
    return total or None


def rates(tasks: Sequence[Ended], *, read: bool = False) -> dict[str, list[int]]:
    """Per cell, [passed, tried] over its first tries. A cell a person pinned says nothing of
    where the router should start, so a pinned task teaches none. With `read`, a first try the
    independent check never read (`Ended.unread`) is left out: a pass nobody looked at is no
    evidence for a cheaper start."""
    out: dict[str, list[int]] = {}
    for task in tasks:
        if task.pinned:
            continue
        for i, (cell, passed) in enumerate(samples(task.attempts)):
            if read and task.unread and i == 0:
                continue
            row = out.setdefault(cell, [0, 0])
            row[0] += int(passed)
            row[1] += 1
    return out


def metrics(tasks: Sequence[Ended]) -> dict[str, Any]:
    """What a set of tasks measured: how many, how many passed, how often the first attempt
    passed, attempts on average, and what a point that passed cost and took, the tasks that
    did not pass included."""
    sized = [t for t in tasks if t.band]
    points = sum(t.band or 0 for t in sized if t.passed)
    # A task whose attempts kept no wall time took no time we know of, not zero: its points
    # stay out of the time per point instead of making it look faster.
    timed = [t for t in sized if t.wall_s > 0]
    timed_points = sum(t.band or 0 for t in timed if t.passed)
    first = [f for f in (t.first for t in tasks) if f is not None]
    return {
        "tasks": len(tasks), "passed": sum(t.passed for t in tasks),
        "pass_first": round(sum(first) / len(first), 3) if first else None,
        "attempts": round(sum(len(t.attempts) for t in tasks) / len(tasks), 2) if tasks else None,
        "cost_per_point": round(sum(t.cost_usd for t in sized) / points, 4) if points else None,
        "time_per_point_s": round(sum(t.wall_s for t in timed) / timed_points, 1) if timed_points else None,
    }


def size_ratio(store: Store, repo: str | None, kind: str) -> tuple[float | None, int]:
    """How much bigger than estimated the kind's recent tasks here turned out: the median of
    realized over estimated points over the last `WINDOW` that had both, and how many."""
    if not repo:
        return None, 0
    ratios = sorted([t.realized / t.band for t in ended(store, repo=repo)
                     if t.kind == kind and t.band and t.realized][-WINDOW:])
    if not ratios:
        return None, 0
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return round(median, 2), len(ratios)


def _position(rungs: Sequence[Cell], cell: Cell | None) -> int:
    if cell is None:
        return 0
    if cell in rungs:
        return rungs.index(cell)
    below = [i for i, c in enumerate(rungs) if c.sort_key() <= cell.sort_key()]
    return below[-1] if below else 0


def _qualified(rungs: Sequence[Cell], cells: Mapping[str, Sequence[int]]) -> list[int]:
    return [i for i, c in enumerate(rungs) if c.label in cells and cells[c.label][1] >= MIN_SAMPLES
            and cells[c.label][0] / cells[c.label][1] >= PASS_FIRST]


def start_cell(ladder: Sequence[Cell], current: Cell | None, measured: Mapping[str, Sequence[int]],
               home: Cell | None = None,
               read: Mapping[str, Sequence[int]] | None = None) -> tuple[Cell | None, dict[str, Any]]:
    """Where the start cell moves (None: it stays), and the evidence. `current` None is
    locks' own rules, which start at `home`, the kind's ladder's first rung (the first of
    `ladder` when none is given: a reach that holds a probation cell starts above it). A move
    to a cheaper rung is made on `read` when given: the first tries the independent check read
    (`rates(read=True)`), since a pass its sampling let through unchecked is the very thing a
    cheaper start would make more of."""
    rungs = [c for c in ladder if not c.needs_approval]
    cells = {c.label: list(measured[c.label]) for c in rungs if c.label in measured}
    evidence: dict[str, Any] = {"cells": cells, "rates": {k: round(p / n, 3) for k, (p, n) in cells.items() if n},
                                "threshold": PASS_FIRST, "min_samples": MIN_SAMPLES, "target": None}
    qualified = _qualified(rungs, cells)
    if not qualified:
        return None, evidence
    target, at = qualified[0], _position(rungs, current if current is not None else home)
    evidence["target"] = rungs[target].label
    if target < at and read is not None:
        seen = {c.label: list(read[c.label]) for c in rungs if c.label in read}
        evidence["read"] = seen
        held = _qualified(rungs, seen)
        if not held or held[0] >= at:
            evidence["held"] = ("a cheaper start needs first tries the independent check read, and the ones "
                                "it did not read are left out")
            return None, evidence
        target = held[0]
        evidence["target"] = rungs[target].label
    if target == at:
        return None, evidence
    return rungs[at + (1 if target > at else -1)], evidence


def rungs_of(kind: str) -> tuple[Cell, ...]:
    """The cells a standard may start a kind on: its reach (`matrix.reach`), a probation cell
    below its ladder included, while the `probation` setting is on; else its ladder."""
    return reach(kind) if config.enabled("probation", os.environ) else ladder_for(kind)


def _rank(values: Sequence[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def start_turns(current: int, needed: Sequence[int], floor: int = 1) -> tuple[int | None, dict[str, Any]]:
    """Where the start turns move (None: they stay), and the evidence.

    Never under `floor`, the kind's own turns: a cap is room, and an attempt that is
    done stops on its own, so a lower one saves nothing and cuts the slow passes.
    """
    evidence: dict[str, Any] = {"samples": len(needed), "min_samples": MIN_SAMPLES, "p75": None, "step": TURNS_STEP}
    if len(needed) < MIN_SAMPLES:
        return None, evidence
    evidence["p75"] = min(_rank(needed, TURNS_PERCENTILE), MAX_TURNS_CEILING)
    aim = max(evidence["p75"], floor)
    if aim == current:
        return None, evidence
    return min(max(current + max(-TURNS_STEP, min(TURNS_STEP, aim - current)), 1), MAX_TURNS_CEILING), evidence


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def _open(store: Store, repo: str) -> list[dict]:
    """The changes in this repository that wait for their verdict (a revert is judged by none)."""
    verdicts = store.verdicts()
    return [c for c in store.calibrations(repo=repo) if c["reverts"] is None and c["id"] not in verdicts]


def _record(store: Store, **change: Any) -> dict[str, Any]:
    change["version"] = __version__
    return {**change, "id": store.add_calibration(**change)}


def calibrate(store: Store, repo: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Measure every band of the repository's standards over its window, and move each by the
    rules: the start cell first, else the start turns; a band whose last change waits for its
    verdict does not move. The changes made, as recorded."""
    at = _stamp(now)
    bands: dict[tuple[str, int], list[Ended]] = defaultdict(list)
    for task in ended(store, repo=repo):
        if task.band and task.kind:
            bands[(task.kind, task.band)].append(task)
    # The sizes whose own band has too few tasks to tell, two or more of them, together: where
    # the kind starts at any size here (`ANY_SIZE`). A band with enough speaks for itself.
    for kind in sorted({k for k, _ in bands}):
        sparse = [b for k, b in bands if k == kind and len(bands[(k, b)][-WINDOW:]) < MIN_SAMPLES]
        if len(sparse) >= 2:
            bands[(kind, ANY_SIZE)] = sorted((t for b in sparse for t in bands[(kind, b)]),
                                             key=lambda t: t.ended_at)
    # A skip waiting for its verdict holds only the skips of its kind (`calibrate_skips`): the
    # ladder is another field, and the start cell at any size moves by its own rules.
    waiting = {(c["kind"], c["band"]) for c in _open(store, repo) if c["field"] != SKIP}
    changes = []
    for (kind, band), tasks in sorted(bands.items()):
        window = tasks[-WINDOW:]
        cells = rates(window)
        row = store.standard(repo, kind, band) or {}
        store.set_standard(repo, kind, band, measured={**metrics(window), "cells": cells})
        if (kind, band) in waiting:
            continue
        moved, evidence = start_cell(rungs_of(kind), _cell(row.get("start_cell")), cells, ladder_for(kind)[0],
                                     read=rates(window, read=True))
        if moved is not None:
            target = evidence["target"]
            rule = (f"the cheapest rung of the {kind} ladder whose first tries passed at least {PASS_FIRST:.0%} of "
                    f"{MIN_SAMPLES} or more is {target}" + ("" if moved.label == target else "; one rung toward it")
                    + (" (its probation cell, below the ladder)" if PROBATION.get(kind) == moved else ""))
            changes.append(_record(store, at=at, repo=repo, kind=kind, band=band, field="start_cell",
                                   old_value=row.get("start_cell"), new_value=moved.label, rule=rule,
                                   evidence={**evidence, "tasks": len(window)}))
            store.set_standard(repo, kind, band, start_cell=moved.label)
            continue
        if band == ANY_SIZE:
            continue
        needed = [n for n in (turns_needed(t.attempts) for t in window if t.passed) if n]
        current = row.get("start_turns")
        turns, evidence = start_turns(current or turns_for(kind), needed, turns_for(kind))
        if turns is not None:
            rule = (f"the 75th percentile of the turns {len(needed)} passing tasks needed is {evidence['p75']}"
                    + ("" if turns == evidence["p75"] else f"; one step of {TURNS_STEP} toward it")
                    + (f", never under the kind's {turns_for(kind)}" if evidence["p75"] < turns_for(kind) else ""))
            changes.append(_record(store, at=at, repo=repo, kind=kind, band=band, field="start_turns",
                                   old_value=str(current) if current else None, new_value=str(turns), rule=rule,
                                   evidence={**evidence, "tasks": len(window)}))
            store.set_standard(repo, kind, band, start_turns=turns)
    return changes + calibrate_skips(store, repo, now=now)


# --- skipping a rung that rarely helps -------------------------------------------------------------

@dataclass(frozen=True)
class Climb:
    """One climb of the ladder in a task: the move, the rung it left and the rung it went to,
    and whether the try on that rung passed (more turns or a retry on it go on with the try)."""

    task: int
    repo: str | None
    move: str
    source: str
    target: str
    passed: bool
    task_passed: bool
    wall_s: float
    ended_at: str


def climbs(task: Ended) -> list[Climb]:
    """The climbs in a task's attempts. A try on the new rung that ended only on the environment
    or a refusal says nothing of the rung: no climb is counted for it."""
    out = []
    attempts = task.attempts
    for i, a in enumerate(attempts[:-1]):
        source, target = _label(a.get("cell")), _label(attempts[i + 1].get("cell"))
        if a.get("move") not in SKIP_MOVES or not source or not target or source == target:
            continue
        passed = said = False
        for b in attempts[i + 1:]:
            if _label(b.get("cell")) != target:
                break
            passed = passed or bool(b["passed"])
            said = said or bool(b["passed"]) or b.get("failure") not in NO_VERDICT
            if b.get("move") not in GOES_ON:
                break
        if said:
            out.append(Climb(task.id, task.repo, str(a["move"]), source, target, passed, task.passed,
                             task.wall_s, task.ended_at))
    return out


def skip_onto(ladder: Sequence[Cell], source: str, target: str) -> Cell | None:
    """Where a climb from `source` goes when `target` is skipped: the rung after it. None when
    there is none (the last rung is never skipped), when either rung needs an approval (fable is
    a person's to start, never a rung a rule lands on or passes over), or when the climb is not
    up the ladder."""
    labels = [c.label for c in ladder]
    if source not in labels or target not in labels:
        return None
    at, to = labels.index(source), labels.index(target)
    if to <= at or to + 1 >= len(ladder):
        return None
    onto = ladder[to + 1]
    if ladder[to].needs_approval or onto.needs_approval:
        return None
    return onto


def active_skips(store: Store, repo: str | None, kind: str) -> dict[tuple[str, str], dict[str, Any]]:
    """The skips in force for a kind here, by the climb they replace (`from`, `to`): each with the
    rung it goes to (`onto`) and the change that made it. A revert takes its change's skip away."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not repo:
        return out
    for change in store.calibrations(repo=repo):
        if change["field"] != SKIP or change["kind"] != kind:
            continue
        value = change["new_value"] if not change["reverts"] else change["old_value"]
        source, _, target = (value or "").partition(">")
        if change["reverts"]:
            out.pop((source, target), None)
            continue
        onto = skip_onto(ladder_for(kind), source, target)
        if onto is not None:
            out[(source, target)] = {"onto": onto, "change": int(change["id"]), "because": because(change)}
        else:
            # The ladder changed under it: the skip no longer has a rung to go to, and stops.
            out.pop((source, target), None)
    return out


def _climb_rate(found: Sequence[Climb]) -> dict[str, Any]:
    passed = sum(c.passed for c in found)
    times = sorted(c.wall_s for c in found if c.task_passed and c.wall_s > 0)
    mid = len(times) // 2
    median = (times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2) if times else None
    return {"climbs": len(found), "passed": passed,
            "pass_first": round(passed / len(found), 3) if found else None,
            "time_to_pass_s": round(median, 1) if median is not None else None}


def calibrate_skips(store: Store, repo: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """For each kind here, skip one rung its climbs rarely pass on: a climb from a rung to the
    next (the same move) whose next try passed under `SKIP_PASS_BELOW` over at least
    `SKIP_MIN_MOVES` of them goes to the rung after instead. The climbs are read in this
    repository, and from every repository only when this one has fewer than `SKIP_MIN_MOVES`
    of that climb (the ladder is code, one per kind); the evidence says which. One skip per kind
    per calibration, and none while another of the kind waits for its verdict. Never the last
    rung, never one that needs an approval, never a retry or more turns, never by cost."""
    at = _stamp(now)
    mine = ended(store, repo=repo)
    everyone: list[Ended] | None = None
    waiting = {c["kind"] for c in _open(store, repo) if c["field"] == SKIP}
    changes = []
    for kind in sorted({t.kind for t in mine if t.kind} - waiting):
        ladder = ladder_for(kind)
        active = active_skips(store, repo, kind)
        here: dict[tuple[str, str, str], list[Climb]] = defaultdict(list)
        for task in mine:
            if task.kind == kind:
                for c in climbs(task):
                    here[(c.move, c.source, c.target)].append(c)
        for (move, source, target) in sorted(here):
            if (source, target) in active or skip_onto(ladder, source, target) is None:
                continue
            found, pooled = here[(move, source, target)][-SKIP_WINDOW:], False
            if len(found) < SKIP_MIN_MOVES:
                if everyone is None:
                    everyone = ended(store)
                found = [c for t in everyone if t.kind == kind for c in climbs(t)
                         if (c.move, c.source, c.target) == (move, source, target)][-SKIP_WINDOW:]
                pooled = True
            measured = _climb_rate(found)
            if measured["climbs"] < SKIP_MIN_MOVES or measured["pass_first"] >= SKIP_PASS_BELOW:
                continue
            onto = skip_onto(ladder, source, target)
            assert onto is not None
            where = f"in every repository ({len({c.repo for c in found})} of them)" if pooled else "here"
            rule = (f"{measured['passed']} of {measured['climbs']} {move} climbs from {source} to {target} "
                    f"passed their next try {where}, under {SKIP_PASS_BELOW:.0%} of {SKIP_MIN_MOVES} or more: "
                    f"climbs from {source} go to {onto.label}")
            changes.append(_record(store, at=at, repo=repo, kind=kind, band=ANY_SIZE, field=SKIP,
                                   old_value=None, new_value=f"{source}>{target}", rule=rule,
                                   evidence={**measured, "move": move, "from": source, "to": target,
                                             "onto": onto.label, "pooled": pooled,
                                             "repos": len({c.repo for c in found}),
                                             "threshold": SKIP_PASS_BELOW, "min_moves": SKIP_MIN_MOVES}))
            break
    return changes


def _judge_skip(store: Store, repo: str, change: Mapping[str, Any], at: str) -> dict[str, Any] | None:
    """A skip, judged once its climbs ran `JUDGE_AFTER` times: the climbs from its rung after it
    (onto the rung after the one skipped) against the climbs into the skipped rung before it, here
    (from every repository when it was made on theirs). Reverted when they pass less by more than
    `REVERT_MARGIN`, or their tasks took more than `SKIP_SLOWER` times as long to pass."""
    evidence = json.loads(change.get("evidence") or "{}")
    source, target, onto = evidence.get("from"), evidence.get("to"), evidence.get("onto")
    kind = change["kind"]
    tasks = [t for t in ended(store, repo=repo) if t.kind == kind]
    after = [c for t in tasks if t.ended_at >= change["at"] for c in climbs(t)
             if c.source == source and c.target == onto][:JUDGE_AFTER]
    if len(after) < JUDGE_AFTER:
        return None
    pool = ended(store) if evidence.get("pooled") else tasks
    before = [c for t in pool if t.kind == kind and t.ended_at < change["at"] for c in climbs(t)
              if c.source == source and c.target == target][-SKIP_WINDOW:]
    then, was = _climb_rate(after), _climb_rate(before)
    fell = was["pass_first"] is not None and round(was["pass_first"] - then["pass_first"], 6) > REVERT_MARGIN
    slower = was["time_to_pass_s"] and then["time_to_pass_s"] and \
        then["time_to_pass_s"] > was["time_to_pass_s"] * SKIP_SLOWER
    worse = bool(fell or slower)
    reason = (f"climbs from {source} passed their next try {_pct(was['pass_first'])} of {was['climbs']} into "
              f"{target} before, {_pct(then['pass_first'])} of {then['climbs']} onto {onto} after; time to pass "
              f"{_secs(was['time_to_pass_s'])} before, {_secs(then['time_to_pass_s'])} after")
    verdict = "reverted" if worse else "kept"
    store.add_verdict(at=at, change_id=change["id"], verdict=verdict, window_before=was, window_after=then,
                      reason=reason, version=__version__)
    if worse:
        why = (f"they passed less by more than {REVERT_MARGIN:.0%}" if fell
               else f"they took more than {SKIP_SLOWER:g} times as long to pass")
        _record(store, at=at, repo=repo, kind=kind, band=change["band"], field=SKIP,
                old_value=change["new_value"], new_value=None, reverts=change["id"],
                rule=f"revert #{change['id']}: {why} ({reason})",
                evidence={"before": was, "after": then, "margin": REVERT_MARGIN, "slower": SKIP_SLOWER})
    return {"change": change["id"], "repo": repo, "verdict": verdict, "before": was, "after": then}


def _onto_probation(change: Mapping[str, Any]) -> bool:
    """A start-cell change that moved a kind onto its probation cell, below its ladder."""
    below = PROBATION.get(change["kind"])
    return change["field"] == "start_cell" and below is not None and change["new_value"] == below.label


def _costlier(change: Mapping[str, Any], before: Mapping[str, Any], then: Mapping[str, Any]) -> bool:
    """Whether a probation start made a passed point dearer than the window before it: the one
    place money moves a standard, and only back up the ladder."""
    return _onto_probation(change) and before["cost_per_point"] is not None and \
        then["cost_per_point"] is not None and then["cost_per_point"] > before["cost_per_point"] * PROBATION_COSTLIER


def _slower(change: Mapping[str, Any], before: Mapping[str, Any], then: Mapping[str, Any]) -> bool:
    """Whether a probation start made a passed point take more than `PROBATION_SLOWER` times as long."""
    return _onto_probation(change) and bool(before["time_per_point_s"]) and bool(then["time_per_point_s"]) and \
        then["time_per_point_s"] > before["time_per_point_s"] * PROBATION_SLOWER


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.3f}"


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value / 60:.0f} min"


def backfill(store: Store, ids: Sequence[int] | None = None) -> int:
    """Give a size to tasks that ended without one (planned before locks kept points, or by
    a plan that failed before it sized them): the same pure estimate a plan makes
    (`points.of_request`), from the task's text, kind and the risk its plan recorded (read
    again from its text when it recorded none), marked as given after the fact. A task with
    no kind stays out. How many were sized."""
    from locks import points as points_mod
    from locks import risk as risk_mod

    if ids is None:
        rows = store.ended_tasks(limit=100000)
    else:
        rows = []
        for task_id in ids:
            task = store.get_task(task_id)
            planned = store.events_of(task_id, "planned") if task else []
            if task:
                rows.append({**task, "planned_data": (planned[0]["data"] or {}) if planned else {}})
    sized = 0
    for row in rows:
        if row.get("points") or not row.get("kind") or row.get("source") not in ("locks", "queue"):
            continue
        recorded = (row["planned_data"] or {}).get("risk") or {}
        risk = (risk_mod.Risk(str(recorded.get("tier") or "medium"), list(recorded.get("reasons") or []))
                if recorded else risk_mod.of_request(row.get("body") or "", row["kind"]))
        score = points_mod.of_request(row.get("body") or row.get("title") or "", row["kind"], risk)
        store.update_task(int(row["id"]), points=score.points,
                          points_why=", ".join([AFTER_THE_FACT, *score.reasons]))
        sized += 1
    return sized


def _cell(label: str | None) -> Cell | None:
    return Cell.parse(label) if _label(label) else None


def judge(store: Store, repo: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Judge each change that ran under `JUDGE_AFTER` tasks: the window before it against the
    tasks after it. Worse pass-first by more than `REVERT_MARGIN` reverts it, one step back,
    recorded as a change that names it; anything else keeps it. The verdicts made."""
    pending = _open(store, repo)
    if not pending:
        return []
    at = _stamp(now)
    tasks = [t for t in ended(store, repo=repo) if t.band]
    out = []
    for change in pending:
        if change["field"] == SKIP:
            # A skip is judged by the climbs it changed, not by tasks that started on it.
            if (verdict := _judge_skip(store, repo, change, at)) is not None:
                out.append(verdict)
            continue
        # A change of the kind at any size is judged by the kind's tasks of every size.
        band = [t for t in tasks if t.kind == change["kind"] and change["band"] in (t.band, ANY_SIZE)]
        # A change carried from a repository that joined a workspace is judged by the tasks
        # that used it under either id.
        ids = {change["id"], json.loads(change.get("evidence") or "{}").get("carried_from")}
        after = [t for t in band if ids & set(t.used)][:JUDGE_AFTER]
        if len(after) < JUDGE_AFTER:
            continue
        before, then = metrics([t for t in band if t.ended_at < change["at"]][-WINDOW:]), metrics(after)
        fell = before["pass_first"] is not None and then["pass_first"] is not None and \
            round(before["pass_first"] - then["pass_first"], 6) > REVERT_MARGIN
        dearer, slower = _costlier(change, before, then), _slower(change, before, then)
        worse = fell or dearer or slower
        verdict = "reverted" if worse else "kept"
        reason = (f"pass-first {_pct(before['pass_first'])} in the {before['tasks']} tasks before, "
                  f"{_pct(then['pass_first'])} in the {then['tasks']} after"
                  + (f"; a passed point cost {_usd(before['cost_per_point'])} before, "
                     f"{_usd(then['cost_per_point'])} after, and took {_secs(before['time_per_point_s'])} before, "
                     f"{_secs(then['time_per_point_s'])} after" if _onto_probation(change) else ""))
        store.add_verdict(at=at, change_id=change["id"], verdict=verdict, window_before=before, window_after=then,
                          reason=reason, version=__version__)
        if worse:
            _record(store, at=at, repo=repo, kind=change["kind"], band=change["band"], field=change["field"],
                    old_value=change["new_value"], new_value=change["old_value"], reverts=change["id"],
                    rule=f"revert #{change['id']}: " + (
                        f"it made pass-first worse by more than {REVERT_MARGIN:.0%}" if fell else
                        "a passed point costs more on the probation cell than on the rung it replaced" if dearer else
                        f"a passed point takes more than {PROBATION_SLOWER:g} times as long on the probation cell")
                    + f" ({reason})",
                    evidence={"before": before, "after": then, "margin": REVERT_MARGIN})
            value = change["old_value"]
            store.set_standard(repo, change["kind"], change["band"], **{change["field"]: (
                int(value) if value and change["field"] == "start_turns" else value)})
        out.append({"change": change["id"], "repo": repo, "verdict": verdict, "before": before, "after": then})
    return out


def settle(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """What the endings since the last settle are due: the verdicts of changes that ran long
    enough, and a calibration of each repository that ended `CALIBRATE_EVERY` tasks since its
    last. In one write transaction: a hook and the dispatcher at the same moment never
    calibrate twice. Runs in the hooks and the dispatcher, never in a GET."""
    calibrated: list[dict[str, Any]] = []
    judged: list[dict[str, Any]] = []
    if not store.meta(BACKFILL_KEY):
        # The first settle on a store with history: what ended before points existed is
        # sized, and every repository with sized endings calibrates now, not after five more.
        with store.immediate():
            if not store.meta(BACKFILL_KEY):
                sized = backfill(store)
                for repo in sorted({t.repo for t in ended(store) if t.repo and t.band}):
                    calibrated += calibrate(store, repo, now=now)
                    store.set_meta(f"standards:since:{repo}", "0")
                store.set_meta(BACKFILL_KEY, json.dumps({"at": _stamp(now), "sized": sized,
                                                         "version": __version__}))
    if not store.endings_after(int(store.meta(SEEN_KEY) or 0)):
        # Nothing ended since: a turn's end takes no write lock for nothing.
        return {"ended": 0, "calibrated": calibrated, "judged": judged}
    with store.immediate():
        endings = store.endings_after(int(store.meta(SEEN_KEY) or 0))
        if not endings:
            return {"ended": 0, "calibrated": calibrated, "judged": judged}
        store.set_meta(SEEN_KEY, str(endings[-1]["id"]))
        repos: dict[str, set[int]] = defaultdict(set)
        for e in endings:
            if e["repo"] and '"investigation"' not in (e["options"] or ""):
                repos[e["repo"]].add(int(e["task_id"]))
        # One that ended with no size (a plan that failed before it sized it) is sized now.
        backfill(store, ids=[int(e["task_id"]) for e in endings])
        for repo, ids in sorted(repos.items()):
            judged += judge(store, repo, now=now)
            count = int(store.meta(f"standards:since:{repo}") or 0) + len(ids)
            if count >= CALIBRATE_EVERY:
                calibrated += calibrate(store, repo, now=now)
                count = 0
            store.set_meta(f"standards:since:{repo}", str(count))
    return {"ended": len(endings), "calibrated": calibrated, "judged": judged}


# --- what a plan takes from its standard, and how a change reads -----------------------------------

def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f} %"


def day(at: str, tz: tzinfo | None = None) -> str:
    """A recorded time as the person reads a date: `Oct 20`, in their time zone (None: this machine's)."""
    moment = datetime.fromisoformat(at).astimezone(tz)
    return f"{moment:%b} {moment.day}"


def size(band: int) -> str:
    if band == ANY_SIZE:
        return "any size"
    return f"{band} point" if band == 1 else f"{band} points"


def because(change: Mapping[str, Any]) -> str:
    """The evidence of a change, in a sentence: "2 of 8 passed first at sonnet/xhigh and 4 of 5
    at opus/high", or the turns passes needed, or what a revert found."""
    evidence = json.loads(change["evidence"]) if isinstance(change["evidence"], str) else change["evidence"]
    if change.get("reverts"):
        return change["rule"]
    if change["field"] == SKIP:
        where = (f" in {evidence.get('repos')} repositories" if evidence.get("pooled") else " here")
        return (f"{evidence.get('passed')} of {evidence.get('climbs')} {evidence.get('move')} climbs from "
                f"{evidence.get('from')} to {evidence.get('to')} passed their next try{where}")
    if change["field"] == "start_cell":
        cells = [f"{p} of {n} " + ("passed first at " if i == 0 else "at ") + label
                 for i, (label, (p, n)) in enumerate((evidence.get("cells") or {}).items())]
        return " and ".join(cells) or change["rule"]
    return (f"the passing tasks needed {evidence.get('p75')} turns at the 75th percentile "
            f"({evidence.get('samples')} of them)")


@dataclass(frozen=True)
class Used:
    """What a plan takes from the standard of its band: its cell and turns (None: locks' own
    rules, or a person's flag that wins), the changes that set them, and why."""

    band: int
    cell: Cell | None = None
    turns: int | None = None
    changes: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    def data(self) -> dict[str, Any]:
        return {"band": self.band, "cell": self.cell.label if self.cell else None, "turns": self.turns,
                "changes": list(self.changes)}


def for_plan(store: Store, repo: str, kind: str, band: int, *, pinned: bool, explicit_turns: bool) -> Used:
    """The standard a task of this kind and size starts on here. A cell a person pinned, or
    turns they asked for, always win; a cell the ladder no longer holds is none."""
    row = store.standard(repo, kind, band) or {}
    latest = {c["field"]: c for c in store.calibrations(repo=repo) if c["kind"] == kind and c["band"] == band}
    name = f"{kind} at {size(band)} here"
    reasons, changes = [], []
    if not row.get("start_cell") and not pinned and band != ANY_SIZE and (
            pooled := store.standard(repo, kind, ANY_SIZE) or {}).get("start_cell"):
        # Its own size has too few tasks to tell: where the kind's tasks of every size here start.
        row = {**row, "start_cell": pooled["start_cell"]}
        latest = {**latest, **{c["field"]: c for c in store.calibrations(repo=repo)
                               if c["kind"] == kind and c["band"] == ANY_SIZE and c["field"] == "start_cell"}}
        name = f"{kind} here (every size: {size(band)} has too few tasks)"
    cell = _cell(row.get("start_cell"))
    if cell is not None and (cell not in rungs_of(kind) or cell.needs_approval):
        reasons.append(f"the standard for {name} says {cell.label}, which the {kind} ladder no longer holds: "
                       "locks' rules choose")
        cell = None
    if cell is not None and pinned:
        reasons.append(f"the standard for {name} starts at {cell.label}; the cell pinned for this task wins")
        cell = None
    turns = row.get("start_turns")
    if turns and explicit_turns:
        reasons.append(f"the standard for {name} starts with {turns} turns; the turns asked for this task win")
        turns = None
    if turns and turns < turns_for(kind):
        # A standard set before the kind's turns rose is room taken away: the kind's own win.
        reasons.append(f"the standard for {name} starts with {turns} turns, under the kind's "
                       f"{turns_for(kind)}: the kind's win")
        turns = None
    for field, value, what in (("start_cell", cell, f"starts at {cell.label if cell else ''}"),
                               ("start_turns", turns, f"starts with {turns} turns")):
        change = latest.get(field)
        if value and change:
            changes.append(int(change["id"]))
            reasons.append(f"standard for {name}: {what} since {day(change['at'])} "
                           f"(change #{change['id']}: {because(change)})")
    if not row.get("start_cell") and not row.get("start_turns") and not pinned:
        # A pinned cell says where it starts; the rules choose only what nobody pinned.
        reasons.append(f"no standard for {name} yet: locks' rules choose"
                       + (f" (measured over {_measured(row)} tasks so far)" if row else ""))
    return Used(band, cell, turns or None, tuple(changes), tuple(reasons))


def _measured(row: Mapping[str, Any]) -> int:
    return int(json.loads(row.get("measured") or "{}").get("tasks") or 0)
