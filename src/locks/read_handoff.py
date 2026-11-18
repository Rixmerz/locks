"""Whether handing a worker the files its session already read helps, tried against its absence.

A session that sends a task often read the very files the task is about. The scribe keeps them
(`scribe.read_handoff`: still unchanged, inside the task's repository), and a brief may name them so
the worker does not search for them again. That is a bet, so it runs as a trial per repository and
nature of work: briefs with files to hand draw `with` or `without` in turns (`locks.arms`) until each
arm holds `TRIAL` first tries; then the files are handed only where their arm passed first tries no
worse than `MARGIN` below the other and reached the first change in fewer calls, at the median, once
each arm has `TIMED` tasks that edited. A tie goes to `without`. A resume keeps its first brief's arm.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from locks import arms as arms_mod
from locks import config
from locks.matrix import READ_ONLY_KINDS
from locks.store import Store

KEY = "read_handoff_arm"
WHY_KEY = "read_handoff_why"
NATURE_KEY = "read_handoff_nature"
#: How many files the brief named (`briefed.read_handoff`); 0 in the `without` arm.
COUNT_KEY = "read_handoff"
ARMS = ("with", "without")
NATURES = ("read", "write")
#: First tries each arm needs before the trial decides, as the primer's.
TRIAL = 12
#: The arm with the files may pass first tries this much less often and still win on calls.
MARGIN = 0.1
#: Tasks that edited each arm needs before calls before the first change decide.
TIMED = arms_mod.TIME_SAMPLES


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("read_handoff", os.environ if env is None else env)


def nature_of(kind: str) -> str:
    return "read" if kind in READ_ONLY_KINDS else "write"


def trial(store: Store, repo: str | None, nature: str | None = None) -> dict[str, dict[str, Any]]:
    """Per arm: tasks, first tries passed (`passed`, `tries`, `rate`), the median calls before the first
    change and from how many tasks that edited, in a repository (None: every one)."""
    out: dict[str, dict[str, Any]] = {}
    rows = [r for r in store.arm_trial(repo, KEY) if nature is None or r.get("nature") == nature]
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        calls = sorted(r["calls_before_edit"] for r in mine if r["calls_before_edit"] is not None)
        passed = sum(bool(r["passed_first"]) for r in mine)
        out[arm] = {"tasks": len(mine), "tries": len(mine), "passed": passed,
                    "rate": round(passed / len(mine), 3) if mine else None,
                    "calls_before_edit_p50": calls[(len(calls) - 1) // 2] if calls else None,
                    "calls_samples": len(calls)}
    return out


def verdict(found: Mapping[str, Mapping[str, Any]], *, where: str = "here") -> tuple[str, str] | None:
    """A full trial's verdict, or None while an arm is short of `TRIAL`."""
    w, wo = found["with"], found["without"]
    if min(w["tries"], wo["tries"]) < TRIAL:
        return None
    timed = min(w["calls_samples"], wo["calls_samples"]) >= TIMED
    said = (f"first tries passed {w['rate']:.0%} with the files, {wo['rate']:.0%} without; calls before the "
            f"first change p50 {w['calls_before_edit_p50']} against {wo['calls_before_edit_p50']}"
            + ("" if timed else f", too few tasks that edited to compare ({TIMED} each needed)"))
    fewer = timed and w["calls_before_edit_p50"] < wo["calls_before_edit_p50"]
    if fewer and w["rate"] >= wo["rate"] - MARGIN:
        return "with", f"the read handoff won its trial {where}: {said}"
    return "without", f"the read handoff lost its trial {where}: {said}{arms_mod.TIE}"


def arm(store: Store, repo: str | None, task_id: int, nature: str) -> tuple[str, str]:
    """Whether this task's brief names the files its session read, and why."""
    if repo is None:
        return "with", "no repository to run a trial in"
    found = trial(store, repo, nature)
    if (got := verdict(found)) is not None:
        return got
    here = f"the {nature} trial here has {found['with']['tasks']} with, {found['without']['tasks']} without, " \
           f"{TRIAL} each"
    if (everywhere := verdict(trial(store, None, nature), where="everywhere")) is not None:
        return everywhere[0], arms_mod.pooled_why(everywhere[1], here)
    chosen = ARMS[arms_mod.side(f"read_handoff:{nature}", repo, task_id,
                                found["with"]["tasks"], found["without"]["tasks"])]
    return chosen, f"the read handoff's trial: {here}"


def arm_for(store: Store, repo: str | None, task_id: int, nature: str) -> tuple[str, str]:
    """The arm the task's first brief drew (a resume keeps it), else the arm it draws now."""
    for event in store.events_of(task_id, "briefed"):
        if (drawn := (event.get("data") or {}).get(KEY)) in ARMS:
            return str(drawn), "kept from its first brief"
    return arm(store, repo, task_id, nature)
