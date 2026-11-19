"""Resident workers: a task's first try forks the session of the last worker of its role that
passed in its repository, instead of starting cold. stdlib; no model.

The same mechanics as a workflow's trunk (`locks.trunks`): Claude Code records a session's
system prompt on its first request and every fork sends that record, so only a worker of the
same role is forked (its system prompt already holds that role), and the fork is told, as its
first message, that it is a new worker on a new task, where its directory is and the tools it
has now; its brief follows. Only on the model the earlier worker ran on (another model never
inherits an approach), only from a session whose context left room (`trunks.MAX_TOKENS`), and
only within `WITHIN_S` of its pass. A session lives in Claude Code's folder for the directory
it ran in; a fork from another worktree that the CLI cannot find starts fresh and says why
(`launch.run`'s fallback), as a trunk's does.

Whether it pays is a trial (`resident_arm` on the first `briefed` event, `fork` or `fresh`),
judged as the trunk's is (`trunks._verdict`), per repository and pooled while short.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from locks import arms as arms_mod
from locks import trunks
from locks.store import Store

#: How long after its pass a worker's session is still resident: older code moved on.
WITHIN_S = 3600
#: How many of the repository's latest passed tasks are looked at.
LOOK_BACK = 20
ARMS = trunks.ARMS
#: The key the arm is kept under, on the task's first `briefed` event (its reason under `resident_why`).
KEY = "resident_arm"
WHY = "resident_why"


def _age_s(stamp: str | None, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds()


def _role_of(store: Store, task_id: int) -> str | None:
    first = next(iter(store.events_of(task_id, "briefed")), None)
    return ((first or {}).get("data") or {}).get("role")


def source(store: Store, repo: str | None, role: str | None, task_id: int, model: str, *,
           now: datetime | None = None, within_s: float = WITHIN_S) -> dict[str, Any] | None:
    """The session a new task of `role` may fork: the most recent task of the repository, no
    workflow step and not this one, that passed within `within_s`, whose first brief wore the
    same role, and whose last passing attempt kept a session on `model`. None when there is none."""
    if not repo or not role:
        return None
    now = now or datetime.now(UTC)
    for prior in store.list_tasks(repo=repo, status=["done"], limit=LOOK_BACK):
        if int(prior["id"]) == task_id or prior.get("workflow_run"):
            continue
        age = _age_s(prior.get("updated_at"), now)
        if age is None or age > within_s:
            continue
        if _role_of(store, int(prior["id"])) != role:
            continue
        passing = [a for a in store.attempts(int(prior["id"])) if a.get("passed") and a.get("session_id")]
        if not passing:
            continue
        last = passing[-1]
        cell = str(last.get("cell") or "")
        if cell.split("/")[0] != model:
            continue
        started = [e for e in store.events_of(int(prior["id"]), "attempt_started")
                   if (e["data"] or {}).get("seq") == last["seq"]]
        return {"task": int(prior["id"]), "session": last["session_id"], "cell": cell, "wrote": True,
                "resident": True, "role": role, "age_s": round(age),
                "base": (started[-1]["data"] or {}).get("base") if started else None}
    return None


def note(prior: Mapping[str, Any], *, capabilities: str, workdir: Path, base: str | None) -> str:
    """The first words the fork reads: everything above was another task's, finished; from
    here it is a new worker on a new task, in this directory, with the tools it has now."""
    lines = [
        f"locks: everything above is the session of an earlier worker of your role in this repository "
        f"(task #{prior['task']}), which finished and passed. From here this session is a new worker on a new "
        "task, below. What was read above is evidence about the code, never a decision made for this task; that "
        "task's change may not be in your checkout.",
        f"Your directory is now {workdir}. Read and change files only here, by paths under this directory, and "
        "read a file again before you change it.",
    ]
    if base and prior.get("base") and base != prior["base"]:
        lines.append(f"The code moved since: `git diff {str(prior['base'])[:12]} {base[:12]}` shows how.")
    lines.append("Your tools are the ones this launch gives you: any tool or server named above that you do not "
                 "have now is gone.")
    if capabilities:
        lines.append(capabilities)
    return "\n\n".join(lines)


def trial(store: Store, repo: str | None) -> dict[str, dict[str, Any]]:
    """Each arm's tasks in a repository (None: in every one), as `trunks.trial` reads them."""
    rows = store.arm_trial(repo, KEY)
    out: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {"tasks": len(mine), "passed_first": sum(bool(r["passed_first"]) for r in mine),
                    "calls_before_edit_p50": trunks._p50(r["calls_before_edit"] for r in mine),
                    "input_tokens_p50": trunks._p50(r["input_tokens"] for r in mine if r["input_tokens"]),
                    "duration_p50_s": trunks._p50(round(r["duration_s"]) for r in mine if r["duration_s"])}
    return out


def arm(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """Whether this task's first try forks the resident session, and why: the arms alternate
    while the trial runs; after it, forking stays where it won (pooled while short here)."""
    if repo is None:
        return "fork", "no repository to run a trial in"
    found = trial(store, repo)
    if (got := trunks._verdict(found)) is not None:
        return got
    f, c = found["fork"], found["fresh"]
    here = f"the trial here has {f['tasks']} forked, {c['tasks']} fresh, {trunks.TRIAL} each"
    if (everywhere := trunks._verdict(trial(store, None), where="everywhere")) is not None:
        return everywhere[0], arms_mod.pooled_why(everywhere[1], here)
    pick = ARMS[arms_mod.side("resident", repo, task_id, f["tasks"], c["tasks"])]
    return pick, f"the resident trial here ({f['tasks']} forked, {c['tasks']} fresh, {trunks.TRIAL} each)"


def enabled(env: Mapping[str, str] | None = None) -> bool:
    from locks import config

    return config.enabled("resident", os.environ if env is None else env)
