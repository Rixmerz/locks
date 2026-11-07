"""A plan read blind before its parts are built, tried against its absence before it stays.

A plan a workflow fans out over answers with parts, and each part is built by a worker that reads
its prompt and the contract, not the planner's reasoning. A part whose prompt does not stand on
its own, or that leaves a design decision (a name, a schema, an endpoint, where a file goes) to its
worker, fails its first try, which is the slowest thing locks does. Nobody reads the plan with a
fresh context before the parts start: this does. A read-only reader (`CELL`) is given the request,
the contract and each part's id, prompt and surfaces, and nothing else; it paraphrases each part,
lists the decisions the prompt leaves open and names the criterion no part answers, each a
`findings` record. When it reports any, the plan goes back once to its own cell, in its own
session, with those lines (`shape`, `PROMPT`); the second answer stands whatever the reader says.
Every reading is a `critiqued` event with its findings, cell, cost and seconds.

Whether it helps is measured, never assumed: a repository's plans with parts alternate between
`with` and `without` the reader (the `briefed` event's `critique_arm`) until each arm has `TRIAL`
first tries, then the reader runs only where the first tries of the steps that came after a plan
read first passed no less often (`arm`), judged as the pre-mortem is (`methods`), since a plan's own
pass is its answer. A resume keeps the arm its first brief drew (`arm_for`).
"""
from __future__ import annotations

import builtins
import dataclasses
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from locks import arms as arms_mod
from locks import config, grants, launch, methods, models
from locks import criteria as criteria_mod
from locks import findings as findings_mod
from locks.matrix import Cell
from locks.store import Store

KEY = "critique_arm"
WHY_KEY = "critique_why"
ARMS = ("with", "without")
#: Plans with parts per arm before the trial decides.
TRIAL = 6
#: The reader's own turns: it reads the plan and a few files, never builds.
TURNS = 40
CELL = Cell("sonnet", "low")
#: The prefix of `WorkerResult.shape` for a plan held back for its reader's findings.
SHAPE = "critique"
#: What the plan is told when it goes back, below the findings.
PROMPT = ("A fresh reader of your plan could not carry out these parts without deciding for themselves; "
          "rewrite the parts so each stands alone, then answer again in the same contract.")

TASK = """
You are a fresh reader of a plan whose parts are not built yet. Each part will be built by a worker that reads \
only that part's prompt and the contract, never the planner's reasoning. Below are the request, the contract and \
the parts. Do not build anything.

(a) Paraphrase each part in two sentences.
(b) For each part, list the design decisions its prompt leaves to its worker: a name, a schema, an endpoint, an \
error-handling strategy, where a file goes.
(c) Name any criterion of the request (S1, S2, ...) that no part answers.
(d) Report each one as a record in `findings`: `where` is the part's id, or `request` for (c); `what` is the \
decision left open or the criterion unanswered; `evidence_class` and `proof` as the result contract says.

A part you could not paraphrase is a finding. You may read the repository to check that the files and symbols a \
part names exist. When every part stands alone, answer `pass` with no findings.
""".strip()


@dataclass(frozen=True)
class Critique:
    findings: tuple[dict[str, str], ...]
    cost_usd: float
    seconds: float
    #: The reader's own verdict word; "" when it gave none.
    verdict: str


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("critique", os.environ if env is None else env)


def due(parts: Sequence[Mapping[str, Any]], *, pinned: bool, env: Mapping[str, str] | None = None) -> bool:
    """Whether a plan's parts are read before they are built: two or more of them (a plan of one is
    the step run whole), on a start nobody pinned, with the setting on."""
    return len(parts) >= 2 and not pinned and enabled(env)


def brief(text: str, parts: Sequence[Mapping[str, Any]], contract: str) -> str:
    """The reader's brief: what to do, then the request, its criteria, the contract and the parts,
    verbatim and nothing else."""
    criteria = criteria_mod.brief(text)
    shown = "\n\n".join(
        f"Part `{part['id']}`\nsurfaces: {json.dumps(list(part.get('surfaces') or []))}\nprompt:\n{part['prompt']}"
        for part in parts)
    return "\n\n".join([TASK, f"The request, verbatim:\n{text}", *([criteria] if criteria else []),
                        f"The contract every part keeps, verbatim:\n{contract}", f"The parts:\n\n{shown}"])


def spec_for(spec: launch.LaunchSpec, text: str, parts: Sequence[Mapping[str, Any]], contract: str
             ) -> launch.LaunchSpec:
    """The reader's launch, from the plan's: its cell and brief, read-only, no capability, its own
    session and turns, and its calls marked as the reader's so they are never read as the plan's."""
    env = dict(spec.env)
    if env.get("LOCKS_WORKER_ATTEMPT"):
        env["LOCKS_WORKER_ATTEMPT"] += "-critique"
    return dataclasses.replace(
        spec, prompt=brief(text, parts, contract), cell=CELL,
        model_id=models.pinned(CELL.model), permission_mode=grants.mode_for(CELL.model),
        judging=True, report=False, parts=False, criteria=(), verdicts=(), verdict_words=(),
        disallowed_tools=tuple(dict.fromkeys([*spec.disallowed_tools, *launch.WRITE_TOOLS])),
        mcp_servers={}, allowed_tools=tuple(r for r in spec.allowed_tools if not r.startswith("mcp__")),
        append_system_prompt="", max_turns=TURNS, timeout_s=launch.timeout_for(TURNS),
        resume_session="", fork_session=False, resume_note="", persist_session=False, env=env)


def read(launcher: Callable[[launch.LaunchSpec], launch.WorkerResult], spec: launch.LaunchSpec, text: str,
         parts: Sequence[Mapping[str, Any]], contract: str, *, store: Store, task_id: int, nth: int,
         round: int) -> Critique:
    """Launch the reader once and keep what it said as a `critiqued` event. A reader that broke
    (a launch that errored or timed out, an answer without exactly one result block) gives no
    findings: the plan goes on, and the event says the reader found nothing."""
    mark = time.monotonic()
    result = launcher(spec_for(spec, text, parts, contract))
    seconds = time.monotonic() - mark
    broke = result.transient or bool(result.shape)
    found = () if broke else findings_mod.read({"findings": list(result.findings)})
    store.add_event(task_id, "critiqued", seq=nth, round=round, cell=CELL.label, findings=list(found),
                    cost_usd=builtins.round(result.cost_usd, 4), seconds=builtins.round(seconds, 2),
                    verdict=result.verdict, held=round == 1 and bool(found))
    return Critique(found, result.cost_usd, seconds, result.verdict)


def shape(found: Sequence[Mapping[str, str]]) -> str:
    """Why a plan that passed goes back: the reader's findings, one line each."""
    return f"{SHAPE}: " + "\n".join(f"{f['where']}: {f['what']}" for f in found)


def trial(store: Store, repo: str | None) -> dict[str, dict[str, Any]]:
    """Per arm: the plans with parts in it and how many first tries of the steps after them passed."""
    rows = []
    for row in store.arm_trial(repo, KEY):
        finished = store.final_event(int(row["task_id"]))
        if len(((finished or {}).get("data") or {}).get("parts") or []) < 2:
            continue  # a plan of one part is never read, so it is in no arm
        tries = methods._first_tries_after(store, int(row["task_id"]))
        # The steps come after the plan and are untimed here, as a pre-mortem's are.
        rows.append({"arm": row["arm"], "tries": len(tries), "passed": sum(tries), "duration_s": None})
    return arms_mod.tally(rows, ARMS)


def _drawn(store: Store, task_id: int) -> str | None:
    """The arm a task's first brief drew."""
    for event in store.events_of(task_id, "briefed"):
        if (drawn := (event.get("data") or {}).get(KEY)) in ARMS:
            return str(drawn)
    return None


def arm(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """Whether this plan's parts are read before they are built, and why: in turns during the
    trial, the arm with fewer plans next; after it, `with` where the steps after read plans passed
    their first tries no less often than after the others."""
    if repo is None:
        return "with", "no repository to run a trial in"
    return arms_mod.pick(trial(store, repo), ARMS, task_id, TRIAL, "critique", repo, pooled=trial(store, None))


def arm_for(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """The arm the task's first brief drew (a resume keeps it), else the arm it draws now."""
    if (kept := _drawn(store, task_id)) is not None:
        return kept, "kept from its first brief"
    return arm(store, repo, task_id)
