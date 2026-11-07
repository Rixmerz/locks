"""Where a task shaped like ones that failed their first try here starts: one rung up, only where the
record shows the rung above rescued those failures.

A first try that fails is the slowest thing locks does: the attempt, its check, the climb and a second
worker's orientation. The standards move a kind's start per size band on its pass-first rate; this
reads a finer shape (the kind, the size, the risk tier and how many areas the request reaches: what
the plan knows before any worker runs, `shape`) and raises the start one rung for a task of a shape
that failed its first tries at that start here, under `BELOW` over `SAMPLES` or more, and only when
the climbs from that start to the rung above passed at least `RESCUED` of them over `RESCUE_SAMPLES`
or more: a rung that did not rescue the shape's failures is no prediction, however often the start
failed. Measured against history before it is used, never a guess: the backtest is the rule.

Only a raise, never a lower start; never a pinned start, a probation start or a reading kind (its
pass is its answer). The plan says why, its `planned` event keeps the prediction with its evidence
(`predicted`), and `locks stats` compares the first tries of predicted starts with the shape's record
at the start they left (observed). Standard library only, no model.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from locks.matrix import READ_ONLY_KINDS, Cell
from locks.store import Store

#: First tries of a shape at a start cell before its failures count, and the pass-first under which
#: they are failures of the start rather than the shape's own odds.
SAMPLES = 4
BELOW = 0.5
#: Climbs from that start to the rung above that must have passed, and over how many, before the rung
#: above is the shape's start: the backtest.
RESCUE_SAMPLES = 2
RESCUED = 0.5
#: Tasks of a kind read back for a shape, newest first.
HISTORY = 60

KEY = "predicted"


def shape(kind: str, points: int, tier: str, areas: int) -> dict[str, Any]:
    """What a task looks like before it runs: its kind, size, risk tier and how many areas it reaches
    (two or more read as several). Every field is on the `planned` event of every task, so older tasks
    are read for it too."""
    return {"kind": kind, "points": int(points), "tier": tier, "areas": "several" if areas >= 2 else "one"}


def of_planned(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """A task's shape from its `planned` event, or None when it kept too little."""
    kind, points, risk = data.get("kind"), data.get("points"), data.get("risk") or {}
    if not kind or not isinstance(points, int) or not isinstance(risk, Mapping):
        return None
    reasons = [str(r) for r in (risk.get("reasons") or ())]
    areas = len({r.split(" ", 1)[0] for r in reasons if r.startswith("about ")})
    return shape(str(kind), points, str(risk.get("tier") or "medium"), areas)


def history(store: Store, repo: str, kind: str, *, limit: int = HISTORY) -> list[dict[str, Any]]:
    """Each recent task of a kind here, with its shape, the cell it started at, whether its first try
    passed, and the cell and outcome of the attempt after it when it climbed (the rescue, if any).
    A task with no attempt, a pinned start or no shape is left out."""
    out: list[dict[str, Any]] = []
    for task in store.list_tasks(repo=repo, status=["done", "failed", "blocked", "dismissed", "superseded"],
                                 limit=limit, source=("locks", "queue")):
        if task["kind"] != kind or task.get("pinned"):
            continue
        planned = store.last_event(task["id"], "planned")
        attempts = store.attempts(task["id"])
        if not planned or not attempts or planned["data"].get("probation"):
            continue
        found = of_planned(planned["data"])
        if found is None:
            continue
        first = attempts[0]
        after = attempts[1] if len(attempts) > 1 else None
        out.append({"task": task["id"], "shape": found, "start": first["cell"], "passed_first": bool(first["passed"]),
                    "next": after["cell"] if after else None, "passed_next": bool(after["passed"]) if after else None,
                    "predicted": bool(planned["data"].get(KEY))})
    return out


def record(rows: Sequence[Mapping[str, Any]], found: Mapping[str, Any], start: str,
           above: str | None) -> dict[str, Any]:
    """The shape's record at a start cell: its first tries there and the ones that passed, and of the
    ones that failed and climbed to `above`, how many passed there."""
    mine = [r for r in rows if r["shape"] == found and r["start"] == start and not r["predicted"]]
    rescued = [r for r in mine if not r["passed_first"] and above and r["next"] == above]
    return {"tries": len(mine), "passed": sum(r["passed_first"] for r in mine),
            "climbs": len(rescued), "rescued": sum(bool(r["passed_next"]) for r in rescued)}


def start(store: Store, repo: str | None, kind: str, found: Mapping[str, Any], cell: Cell,
          ladder: Sequence[Cell]) -> tuple[Cell, dict[str, Any]] | None:
    """The rung above `cell` and the evidence, when tasks of this shape failed their first tries at
    `cell` here and the rung above rescued them; None otherwise, or for a reading kind, no repository,
    or a start at the ladder's top."""
    if repo is None or kind in READ_ONLY_KINDS or cell not in ladder:
        return None
    at = ladder.index(cell)
    if at + 1 >= len(ladder) or ladder[at + 1].needs_approval:
        return None
    above = ladder[at + 1]
    seen = record(history(store, repo, kind), found, cell.label, above.label)
    if seen["tries"] < SAMPLES or seen["passed"] / seen["tries"] >= BELOW:
        return None
    if seen["climbs"] < RESCUE_SAMPLES or seen["rescued"] / seen["climbs"] < RESCUED:
        return None
    return above, {"shape": dict(found), "from": cell.label, "to": above.label, **seen}


def line(evidence: Mapping[str, Any]) -> str:
    """Why the start was raised, in the plan's words."""
    s = evidence["shape"]
    return (f"one rung up, predicted from this repository's record: {s['kind']} tasks of {s['points']} points, "
            f"{s['tier']} risk, {s['areas']} area(s) passed their first try {evidence['passed']} of "
            f"{evidence['tries']} time(s) at {evidence['from']}, and the climb to {evidence['to']} rescued "
            f"{evidence['rescued']} of {evidence['climbs']} of them")


def report(store: Store, repo: str | None) -> list[dict[str, Any]]:
    """Per kind and shape: the first tries of predicted starts and how they went, beside the shape's
    record at the start it left (observed, `locks stats`)."""
    out: dict[tuple, dict[str, Any]] = {}
    repos = [repo] if repo else sorted({t["repo"] for t in store.list_tasks(limit=1000, source=("locks", "queue"))
                                        if t.get("repo")})
    for one in repos:
        for kind in sorted({t["kind"] for t in store.list_tasks(repo=one, limit=1000, source=("locks", "queue"))
                            if t.get("kind") and t["kind"] not in READ_ONLY_KINDS}):
            rows = history(store, one, kind)
            for r in rows:
                if not r["predicted"]:
                    continue
                key = (one, kind, tuple(sorted(r["shape"].items())), r["start"])
                row = out.setdefault(key, {"repo": one, "kind": kind, "shape": r["shape"], "start": r["start"],
                                           "predicted": 0, "passed_first": 0})
                row["predicted"] += 1
                row["passed_first"] += int(r["passed_first"])
    return list(out.values())
