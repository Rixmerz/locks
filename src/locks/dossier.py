"""Whether livespec's dossier in a brief helps, tried against its absence.

Where an index is present, a task's first brief draws `dossier` or `none` per repository and nature of
work (reading or writing kinds): the arms alternate until each holds `TRIAL` first tries, then the
dossier is briefed only where its arm passed first tries no less often and no slower (`locks.arms`).
A resume keeps the arm its first brief drew. What locks reads from the index for itself (the size
estimate, the rung rule for reading kinds, the plan's warning of tests outside the surfaces, what
`assess` hands the independent check) is the same in both arms: only the worker's dossier text differs.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from locks import arms as arms_mod
from locks import config
from locks.matrix import READ_ONLY_KINDS
from locks.store import Store

KEY = "dossier_arm"
WHY_KEY = "dossier_why"
NATURE_KEY = "dossier_nature"
ARMS = ("dossier", "none")
NATURES = ("read", "write")
#: First tries each arm needs before the trial decides (the size the handing trial had).
TRIAL = 12


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("dossier_trial", os.environ if env is None else env)


def nature_of(kind: str) -> str:
    """`read` for the reading kinds, `write` for the others."""
    return "read" if kind in READ_ONLY_KINDS else "write"


def trial(store: Store, repo: str | None, nature: str | None = None) -> dict[str, dict[str, Any]]:
    """Per arm: its tasks, first tries passed and the median time of a pass, in a repository (None: in
    every one) and, given a `nature`, for the tasks of that nature of work."""
    rows = [{"arm": r["arm"], "tries": 1, "passed": int(r["passed_first"]), "duration_s": r["duration_s"]}
            for r in store.arm_trial(repo, KEY) if nature is None or r.get("nature") == nature]
    return arms_mod.tally(rows, ARMS)


def arm(store: Store, repo: str | None, task_id: int, nature: str) -> tuple[str, str]:
    """Whether this task's brief carries the dossier, and why."""
    if repo is None:
        return "dossier", "no repository to run a trial in"
    return arms_mod.pick(trial(store, repo, nature), ARMS, task_id, TRIAL, f"{nature} dossier", repo,
                         pooled=trial(store, None, nature))


def arm_for(store: Store, repo: str | None, task_id: int, nature: str) -> tuple[str, str]:
    """The arm the task's first brief drew (a resume keeps it), else the arm it draws now."""
    for event in store.events_of(task_id, "briefed"):
        if (drawn := (event.get("data") or {}).get(KEY)) in ARMS:
            return str(drawn), "kept from its first brief"
    return arm(store, repo, task_id, nature)
