"""Methods a worker is briefed to follow, each tried against its absence before it stays.

A method is a few lines of how to work that a kind's workers get in their brief: a diagnosis
protocol for `debug-unclear` (reproduce, where it fails and where it does not, why until a cause
that can be shown, then the fix), and a pre-mortem for `plan` (how the plan could fail and how
each failure would show). Both come from long-standing problem-solving practice (Is/Is-Not,
Five Whys, failure mode analysis); no text is taken from anyone.

Whether one helps is measured, never assumed: a repository's tasks of its kind alternate
between `with` and `without` it (the `briefed` event's `method_arm`, `<method>:<arm>`) until
each arm has `TRIAL` tasks, then it stays only where its arm did no worse (`arm`). A diagnosis is
judged by its own first tries; a pre-mortem by the first tries of the steps that came after the
plan in its workflow run, since a plan's own pass is its answer. A pre-mortem's risks also reach
the independent check of those later steps as where to probe (`risks`), data and never findings.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from locks import arms as arms_mod
from locks import config
from locks.store import Store

KEY = "method_arm"
ARMS = ("with", "without")
#: Tasks per arm before a method's trial decides.
TRIAL = 6
#: The most risk lines a plan's pre-mortem hands a later step's independent check.
RISKS_SHOWN = 8


@dataclass(frozen=True)
class Method:
    name: str
    kind: str
    lines: str


METHODS: dict[str, Method] = {
    "debug-unclear": Method("diagnosis", "debug-unclear", (
        "Diagnose before you change anything. First reproduce the failure and keep the command that shows it. "
        "Then mark where it fails and where it does not: which inputs, which commits (compare with the base, or "
        "`git bisect` between them), which environments, and what differs between the two sides. Ask why until "
        "you reach a cause you can show (a line, a value, a commit), not a symptom. Only then fix that cause, and "
        "show the failure gone with the same command. Put the cause and its proof in `evidence`.")),
    "plan": Method("premortem", "plan", (
        "Before you finish, imagine this plan was carried out and failed. End your summary with a line `Risks:` "
        "and under it, one per line, how it could fail and how that failure would show (the test, the probe or "
        "the output that would catch it). Those lines are handed to the checks of the work that follows.")),
}


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("methods", os.environ if env is None else env)


def for_kind(kind: str) -> Method | None:
    return METHODS.get(kind)


def _first_tries_after(store: Store, task_id: int) -> list[bool]:
    """The first tries of the steps that came after a plan in its workflow run."""
    task = store.get_task(task_id) or {}
    if not task.get("workflow_run"):
        return []
    out = []
    for later in store.run_tasks(int(task["workflow_run"])):
        if int(later["id"]) <= task_id or later.get("status") not in ("done", "failed", "blocked", "replan"):
            continue
        first = store.attempt(int(later["id"]), 1)
        if first is not None:
            out.append(bool(first["passed"]))
    return out


def trial(store: Store, repo: str | None, method: Method) -> dict[str, dict[str, Any]]:
    """Per arm: the tasks in it and how many first tries passed (a pre-mortem's: of the steps after it)."""
    rows = []
    for row in store.arm_trial(repo, KEY):
        name, _, side = str(row["arm"]).partition(":")
        if name != method.name:
            continue
        tries = _first_tries_after(store, int(row["task_id"])) if method.kind == "plan" else [row["passed_first"]]
        # A diagnosis is timed by its own first try; a pre-mortem's steps come after it, untimed here.
        rows.append({"arm": side, "tries": len(tries), "passed": sum(bool(t) for t in tries),
                     "duration_s": None if method.kind == "plan" else row.get("duration_s")})
    return arms_mod.tally(rows, ARMS)


def arm(store: Store, repo: str | None, method: Method, task_id: int) -> tuple[str, str]:
    """Whether this task's brief gets the method, and why: in turns during its trial, the arm
    with fewer tasks next; after it, where its arm did no worse than the arm without it."""
    if repo is None:
        return "with", "no repository to run a trial in"
    return arms_mod.pick(trial(store, repo, method), ARMS, task_id, TRIAL, method.name, repo,
                         pooled=trial(store, None, method))


_RISKS = re.compile(r"^[ \t]*\**risks\**[ \t]*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)


def risks(text: str) -> list[str]:
    """The lines under a plan's `Risks:` line, as it wrote them, up to `RISKS_SHOWN`."""
    found = _RISKS.search(text or "")
    if not found:
        return []
    rest = [found.group(1), *text[found.end():].splitlines()]
    out = []
    for line in rest:
        line = line.strip().lstrip("-*•0123456789.) ").strip()
        if not line:
            if out:
                break
            continue
        out.append(line)
    return out[:RISKS_SHOWN]


def run_risks(store: Store, task: Mapping[str, Any]) -> list[str]:
    """The risks the plan steps before this one in its workflow run named, for its independent check."""
    if not task.get("workflow_run"):
        return []
    out: list[str] = []
    for step in store.run_tasks(int(task["workflow_run"])):
        if int(step["id"]) >= int(task["id"]) or step.get("kind") != "plan" or step.get("status") != "done":
            continue
        out += [r for r in risks(str(step.get("result") or "")) if r not in out]
    return out[:RISKS_SHOWN]


def lines_for(found: Sequence[str]) -> list[str]:
    return [f"the plan before this work named a risk: {r}" for r in found]
