"""What a neighbour a worker was handed must show before its pass counts, read from the
tool-call log, never from the worker's words; and whether holding workers to it pays.

A hint in a worker's system prompt is the weakest way to have a tool used: a worker may read
it and never call the tool. A requirement (`REQUIRED`) names a neighbour and when its use is
owed: layout-inspector on writing work whose change touches rendered UI (`capabilities.renders`: a
`UI_FILES` path that is no test, fixture, example, documentation or tool output),
A passing attempt that owes one is held back when no
call of that neighbour's tools went through in the task's attempts so far (`owed`, from
`tool_events`, its `ok`): the first time the same cell goes on in its own session, told what to
measure (`PROMPT`, as a reshape); the second it stands as work with one point, a `decision` for
the orchestrator, kept on its branch. Never a climb: a stronger model measures nothing a weaker
one would not. A worker held to it is told so in its brief, as a requirement (`brief_line`).

Whether it pays is measured, never assumed: a repository's tasks that were handed a required
neighbour alternate between `gate` and `free` (the `briefed` event's `evidence_arm`) until each
arm has `TRIAL` tasks the independent check read; then the gate stays only where the passes it
held drew the check's blockers less often than the free arm's (`arm`). `locks stats` shows both.
Every handed neighbour's calls are also counted per attempt (`calls`, the `attempt_finished`
event's `capability_calls`), held to it or not, so "handed but never called" is seen.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from locks import arms as arms_mod
from locks import capabilities, config
from locks.matrix import READ_ONLY_KINDS
from locks.store import Store

#: The `briefed` event's field that keeps a task's arm of the trial.
KEY = "evidence_arm"
ARMS = ("gate", "free")
#: Tasks the independent check read, per arm, before the trial decides.
TRIAL = 8
#: How a reshape for evidence is told apart from one for form (`orchestrate._reshape_prompt`).
SHAPE = "evidence"
PROMPT = ("Your work so far stands and is in this directory: do not redo it. Your pass was held back because "
          "no call of the tools named below went through, and this repository holds that work to what they "
          "measure. Use them now as their hint says, fix what they show, and answer again in the result contract "
          "you were given, with what they measured in `evidence`. If they cannot run here, say exactly why in "
          "`evidence` (the command and its output).")


@dataclass(frozen=True)
class Requirement:
    """A neighbour whose use a passing attempt owes, and when."""

    capability: str
    #: Owed only when the change touches rendered UI.
    ui_only: bool
    #: What the worker is told to show, in the brief and the reshape.
    what: str


REQUIRED: tuple[Requirement, ...] = (
    Requirement("layout-inspector", True,
                "a measurement of the rendered page by layout-inspector (detect_issues, or compare_viewports for "
                "every breakpoint), taken after your last change"),
)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("evidence", os.environ if env is None else env)


def handed(names: Sequence[str]) -> list[Requirement]:
    """The requirements whose neighbour a worker was handed."""
    return [r for r in REQUIRED if r.capability in names]


def brief_line(found: Sequence[Requirement]) -> str:
    """What a worker held to evidence is told before it starts: a requirement, not a hint."""
    return ("This work is held to evidence. Your pass counts only with "
            + "; and ".join(r.what for r in found)
            + ". locks reads whether those tools answered from its own log of your calls, never from what you "
              "write; a pass without them goes back to you.")


def owed(store: Store, task_id: int, nth: int, names: Sequence[str], changed: Sequence[str],
         kind: str) -> list[Requirement]:
    """The requirements a passing attempt has not met: a neighbour it was handed whose condition
    holds (writing work; for a UI one, a change that touches rendered UI) and none of whose tools
    answered in this task's attempts so far. The hooks' log is read into the store first."""
    if kind in READ_ONLY_KINDS:
        return []
    from locks import habits

    with contextlib.suppress(OSError):
        habits.load_events(store)
    out = []
    for r in handed(names):
        if r.ui_only and not any(capabilities.renders(p) for p in changed):
            continue
        prefix = f"mcp__{r.capability}__"
        if not any(store.tool_counts(task_id, n, prefix, ok_only=True) for n in range(1, nth + 1)):
            out.append(r)
    return out


def shape(found: Sequence[Requirement]) -> str:
    """Why a pass that owes evidence is held back, in the words its worker is given."""
    return f"{SHAPE}: no call of {', '.join(r.capability for r in found)} went through; this work owes " + \
        "; and ".join(r.what for r in found)


def calls(store: Store, task_id: int, nth: int, names: Sequence[str]) -> dict[str, dict[str, int]]:
    """Each capability an attempt was handed, and the calls of its tools its worker made: `calls`
    in all and `ok` the ones that went through. A capability handed and never called reads 0."""
    from locks import habits

    with contextlib.suppress(OSError):
        habits.load_events(store)
    out = {}
    for name in names:
        prefix = f"mcp__{name}__"
        made = sum(store.tool_counts(task_id, nth, prefix).values())
        through = sum(store.tool_counts(task_id, nth, prefix, ok_only=True).values())
        out[name] = {"calls": made, "ok": through}
    return out


def trial(store: Store, repo: str | None) -> dict[str, dict[str, Any]]:
    """Per arm: the tasks in it, the ones the independent check read, and how many of those it
    blocked (each task's first decided check, `trust.inspections`)."""
    from locks import trust

    rows = store.arm_trial(repo, KEY)
    read = {i.task: i.outcome for i in trust.inspections(store, repo) if i.outcome != "sampled"}
    out: dict[str, dict[str, Any]] = {a: {"tasks": 0, "read": 0, "blocked": 0, "passed_first": 0} for a in ARMS}
    for row in rows:
        arm = out.get(row["arm"])
        if arm is None:
            continue
        arm["tasks"] += 1
        arm["passed_first"] += int(row["passed_first"])
        if row["task_id"] in read:
            arm["read"] += 1
            arm["blocked"] += int(read[row["task_id"]] == "blocked")
    for arm in out.values():
        arm["blocked_rate"] = round(arm["blocked"] / arm["read"], 3) if arm["read"] else None
    return out


def arm(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """Whether a task handed a required neighbour is held to its evidence, and why. During the
    trial the arms alternate, the one with fewer tasks next; after it the gate stays where the
    passes it held drew the independent check's blockers less often than the free arm's."""
    if repo is None:
        return "gate", "no repository to run a trial in: held to evidence"
    found = trial(store, repo)
    g, f = found["gate"], found["free"]
    if (got := _verdict(found)) is not None:
        return got
    here = f"the trial here has {g['read']} held and {f['read']} free read by the independent check, {TRIAL} each"
    if (everywhere := _verdict(trial(store, None), where="everywhere")) is not None:
        return everywhere[0], arms_mod.pooled_why(everywhere[1], here)
    pick = ARMS[arms_mod.side("evidence", repo, task_id, g["tasks"], f["tasks"])]
    return pick, (f"the evidence trial here ({g['read']} held and {f['read']} free read by the independent "
                  f"check, {TRIAL} each)")


def _verdict(found: Mapping[str, Mapping[str, Any]], *, where: str = "here") -> tuple[str, str] | None:
    """A trial's verdict, or None while the independent check read fewer than `TRIAL` of either arm."""
    g, f = found["gate"], found["free"]
    if min(g["read"], f["read"]) < TRIAL:
        return None
    said = (f"the independent check blocked {g['blocked']} of {g['read']} passes held to evidence and "
            f"{f['blocked']} of {f['read']} free")
    if g["blocked_rate"] < f["blocked_rate"]:
        return "gate", f"holding work to evidence won its trial {where}: {said}"
    return "free", f"holding work to evidence lost its trial {where}: {said}"
