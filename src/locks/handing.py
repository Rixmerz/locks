"""livespec handed to a worker, tried against withholding it, per repository and nature of work.

A worker handed livespec gets its server (started with the worker), the tools' descriptions in
every turn's context, the orientation lines and the hint. Whether that pays is measured, never
assumed: 6 workers in a row were handed it and none called it. A repository's tasks of each nature
(reading, writing) alternate between `hand` and `withhold` (the `briefed` event's `livespec_arm`,
`<nature>:<arm>`) until each arm has `TRIAL` first tries; then livespec stays handed only where its
arm passed no less often and, over `arms.TIME_SAMPLES` timed passes each, no slower than
`arms.SLOWER` times the other arm's median (`arm`). What locks does with the index itself (the code
map briefed, the size estimate, what the change reaches briefed to the independent check) is never
withheld: the trial is of the tools in the worker's hands. A resume keeps the arm its first brief
drew. The `livespec_trial` setting turns it off: every worker is handed it, as before.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from locks import arms as arms_mod
from locks import config
from locks.matrix import READ_ONLY_KINDS
from locks.store import Store

KEY = "livespec_arm"
ARMS = ("hand", "withhold")
#: First tries per arm before a nature's trial decides.
TRIAL = 6
NATURES = ("writing", "reading")


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("livespec_trial", os.environ if env is None else env)


def nature(kind: str) -> str:
    return "reading" if kind in READ_ONLY_KINDS else "writing"


def trial(store: Store, repo: str | None, which: str) -> dict[str, dict[str, Any]]:
    """Per arm of one nature: its tasks, first tries, the passed ones and the median time of a pass."""
    rows = []
    for row in store.arm_trial(repo, KEY):
        side_nature, _, side = str(row["arm"]).partition(":")
        if side_nature == which:
            rows.append({"arm": side, "tries": 1, "passed": int(bool(row["passed_first"])),
                         "duration_s": row.get("duration_s")})
    return arms_mod.tally(rows, ARMS)


def drawn(store: Store, task_id: int) -> str | None:
    """The arm a task's first brief drew, kept for every later brief of it (a resume's too)."""
    for event in store.events_of(task_id, "briefed"):
        if (arm := (event.get("data") or {}).get(KEY)) and ":" in str(arm):
            return str(arm).partition(":")[2]
    return None


def arm(store: Store, repo: str | None, kind: str, task_id: int) -> tuple[str, str]:
    """Whether this task's worker is handed livespec, and why: the arm its first brief drew; else in
    turns during the trial of its nature here, the arm with fewer tasks next; after it, `hand` where
    handed workers passed their first tries no less often and no slower than `withhold`'s."""
    if repo is None:
        return "hand", "no repository to run a trial in"
    if (kept := drawn(store, task_id)) in ARMS:
        return kept, "the arm this task's first brief drew"
    which = nature(kind)
    return arms_mod.pick(trial(store, repo, which), ARMS, task_id, TRIAL, f"livespec-{which}", repo,
                         pooled=trial(store, None, which))
