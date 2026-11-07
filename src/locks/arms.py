"""Two arms, tried in turns until each has enough first tries, then the one that did no worse.

The shape every measured choice in locks shares (a method, the order of a brief's notes): tasks
alternate between an arm `with` the change and one without it, the arm with fewer tasks next,
until each holds `trial` first tries; then the change stays only where its arm passed its first
tries no less often and, once `TIME_SAMPLES` passes each are timed, no slower than `SLOWER` times
the other arm's median (a change that passes as often but takes longer is no gain). No model, no
clock: the record of first tries decides.
"""
from __future__ import annotations

import hashlib
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Passing first tries each arm needs before their time to pass counts in the verdict, and how much
#: slower, at the median, the arm with the change may be and still stay: a change that passes as
#: often but takes longer is no gain (the person's clock is the point of a first try).
TIME_SAMPLES = 3
SLOWER = 1.25

#: How far apart two arms' task counts may be before the one with fewer goes next. Under it the
#: task's hash decides, salted with the trial's name: two trials over the same tasks (the primer and
#: a role's lessons, a method and the recall order) then land their arms on different tasks, where
#: one parity shared by every trial put the same tasks in every "with" arm and made each trial's
#: effect read as every other's.
BALANCE = 2


def side(name: str, repo: str | None, task_id: int, with_n: int, without_n: int) -> int:
    """Which arm a task goes in: 0 the change, 1 its absence. The arm with fewer tasks once they
    differ by `BALANCE` or more; else the parity of a hash of the trial, the repository and the
    task, so no two trials share it and no clock enters."""
    if abs(with_n - without_n) >= BALANCE:
        return 0 if with_n < without_n else 1
    digest = hashlib.sha256(f"{name}|{repo or ''}|{task_id}".encode()).digest()
    return digest[0] % 2


def tally(rows: Iterable[Mapping[str, Any]], arms: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Per arm: its tasks, first tries and the ones that passed, from rows of (`arm`, `tries`, `passed`),
    and the median time its passing first tries took (`pass_p50_s`, from a row's `duration_s` when it
    passed; None under `TIME_SAMPLES` of them)."""
    out: dict[str, dict[str, Any]] = {a: {"tasks": 0, "tries": 0, "passed": 0, "_times": []} for a in arms}
    for row in rows:
        side = out.get(str(row["arm"]))
        if side is None:
            continue
        side["tasks"] += 1
        side["tries"] += int(row["tries"])
        side["passed"] += int(row["passed"])
        took = row.get("duration_s")
        if took and int(row["passed"]) and int(row["tries"]) == 1:
            side["_times"].append(float(took))
    for side in out.values():
        side["rate"] = round(side["passed"] / side["tries"], 3) if side["tries"] else None
        times = side.pop("_times")
        side["pass_p50_s"] = round(float(statistics.median(times)), 1) if len(times) >= TIME_SAMPLES else None
    return out


def pick(found: Mapping[str, Mapping[str, Any]], arms: Sequence[str], task_id: int, trial: int,
         name: str, repo: str | None = None) -> tuple[str, str]:
    """The arm this task goes in, and why: `arms[0]` is the change, `arms[1]` its absence."""
    on, off = arms
    w, wo = found[on], found[off]
    if min(w["tries"], wo["tries"]) < trial:
        chosen = arms[side(name, repo, task_id, w["tasks"], wo["tasks"])]
        return chosen, f"the {name} trial here ({w['tries']} {on}, {wo['tries']} {off}, {trial} each)"
    said = f"first tries passed {w['rate']:.0%} {on} and {wo['rate']:.0%} {off}"
    if w["rate"] < wo["rate"]:
        return off, f"the {name} lost its trial here: {said}"
    slow, fast = w.get("pass_p50_s"), wo.get("pass_p50_s")
    if slow is not None and fast is not None and slow > fast * SLOWER:
        return off, (f"the {name} lost its trial here: {said}, and the passes took {slow:.0f}s at the median "
                     f"{on} against {fast:.0f}s {off}")
    timed = f", passes took {slow:.0f}s {on} and {fast:.0f}s {off}" if slow is not None and fast is not None else ""
    return on, f"the {name} won its trial here: {said}{timed}"
