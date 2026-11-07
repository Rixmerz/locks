"""The loop: classify, choose a starting cell, attempt, check, move, remember.

Everything here is deterministic code. The models are called for two things
only — to classify a request the rules did not recognise, and to do the work —
and nothing a model says about its own work decides the next move by itself:
the result block is checked for evidence, an optional verify command has the
last word on a claimed pass, and the failure kind is read against the ladder by
`escalate.decide`.

What a report says changed is git's account, printed beside the worker's own:
a person reading "I created the routes" can see on the next line whether any
file says so.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import hashlib
import json
import os
import pickle
import re
import shlex
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks import (
    after,
    behavior,
    cells,
    ci,
    config,
    dispatch,
    grants,
    habits,
    isolate,
    launch,
    manifests,
    models,
    notes,
    plugins,
    preflight,
    project,
    racing,
    repo,
    revisions,
    roles,
    runtime,
    standards,
    stops,
    teams,
    trunks,
    trust,
    workflows,
)
from locks import allow as allow_rules
from locks import capabilities as caps
from locks import criteria as criteria_mod
from locks import evidence as evidence_mod
from locks import findings as findings_mod
from locks import handing as handing_mod
from locks import ledger as ledger_mod
from locks import methods as methods_mod
from locks import notes as notes_mod
from locks import points as points_mod
from locks import predict as predict_mod
from locks import risk as risk_mod
from locks import services as services_mod
from locks import specs as specs_mod
from locks import surfaces as surfaces_mod
from locks import tracer as tracer_mod
from locks.adapters import ABSENT, UNREADABLE, Adapter, Status, default_adapters
from locks.classify import Classification, by_rules, classify, narrow, opens_with_change
from locks.escalate import (
    MAX_TURNS_CEILING,
    REPLAN,
    Attempt,
    Decision,
    Failure,
    Move,
    Skip,
    decide,
    review_reason,
    waited,
)
from locks.matrix import DEFAULT_TURNS, PROBATION, READ_ONLY_KINDS, Cell, ladder_for, reach, turns_for
from locks.store import Store, home
from locks.text import clip, few

#: Failures that describe something tried against the problem, worth keeping
#: as a dead end. A timeout or a turn ceiling says nothing about the fix.
_MEMORABLE = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.APPROACH, Failure.SPEC_BUG,
                        Failure.ARCHITECTURE_BUG})

#: How a brief names where a dead end came from (`locks.cells.scope`).
_SCOPE_SAID = {"shared": "met across areas", "area": "met in this task's area", "role": "met by this role"}


class Cancelled(Exception):
    """Raised into a run when someone cancels it; the run stops and cleans up."""


_FINAL_STATUS = {
    Move.REPLAN: "replan",
    Move.NEEDS_APPROVAL: "needs_approval",
    Move.EXHAUSTED: "failed",
    Move.BLOCKED: "blocked",
    Move.CONVERGED: "failed",
    Move.REVIEW: "blocked",
    Move.DECIDE: "blocked",
}

#: Endings that wait on a person, not on the task being rewritten: the work done
#: so far is kept on the task's branch, unverified, for them to see and resume.
KEEPS_WORK = frozenset({"done", "blocked", "needs_approval"})

#: How many finished tasks of a kind it takes before history moves the start.
MIN_REPO_SAMPLES = 3
MIN_GLOBAL_SAMPLES = 5
#: How many recent tasks of a kind here that outgrew their turns raise where the next one's start,
#: and how many recent tasks are looked at.
MIN_TURN_SAMPLES, TURN_HISTORY = 2, 10
#: A task this large (points) starts on more turns after one earlier task of its kind here outgrew
#: them, not two: a large first try that runs out costs the most to repeat.
LARGE_POINTS = 8
#: How long a task's check may run. A full suite on a loaded machine (many workers at once)
#: measured 19:25 on 2026-10-27, past the old 900 s, and every cut was read as a failure.
VERIFY_TIMEOUT_S = 1800
#: How many changed paths a task's `finished` event lists; `changed_n` counts them all.
CHANGED_KEPT = 500

Launcher = Callable[[launch.LaunchSpec], launch.WorkerResult]


@dataclass
class Options:
    max_attempts: int = 6
    allow_approval: bool = False
    isolate: bool = True
    verify: str | None = None
    launch_dir: Path | None = None
    #: None: the kind's own (`matrix.turns_for`).
    max_turns: int | None = None
    use_model_classifier: bool = True
    kind: str | None = None
    start: Cell | None = None
    dry_run: bool = False
    #: None reads the `livespec` setting (on by default).
    livespec: bool | None = None
    #: Permission rules a person granted the workers, e.g. `Bash(npm run build)`.
    allow_tools: tuple[str, ...] = ()
    #: None reads the `notes` setting (on by default): the project's notes in the
    #: brief, and what a passing worker learned filed into them.
    notes: bool | None = None
    #: The commit a new worktree starts from instead of HEAD: a workflow step
    #: builds on the work of the step before it.
    base: str | None = None
    #: A role a person named for this run: it runs as that role, on the kinds it takes.
    role: str | None = None
    #: Where the task may write (`locks.surfaces`): a change outside is undone and fails the attempt.
    surfaces: tuple[str, ...] = ()
    #: The role a workflow step names: used when it is in the project's team,
    #: since a definition a repository commits must not choose beyond the person's team.
    step_role: str | None = None
    #: A workflow step's part in a review ledger (`locks.ledger`): its role, round and rows.
    ledger: dict | None = None
    #: What a passing worker learned is filed in the project's notes (and engram, when
    #: writing there is on). Off for a task whose findings wait on a person's agreement.
    keep_learned: bool = True
    #: Bring the person's uncommitted changes into a new worktree: work the task
    #: builds on that no commit holds yet.
    carry: bool = False
    #: Directories beside the work a worker may read and use (a sibling repository).
    add_dirs: tuple[str, ...] = ()
    #: Servers the task needs up, `command@port`, started and stopped by locks
    #: around the run (`locks.services`): the person's flag, never a file's.
    services: tuple[str, ...] = ()
    #: A workflow run's check, for a step classified when it runs: it becomes the
    #: step's `verify` only when the step turns out to write.
    run_verify: str | None = None
    #: A plan step a workflow fans out over: its pass carries the parts and their contract.
    parts: bool = False
    #: A workflow's integrate step: the parts' branches it joins, in the plan's order
    #: (`{part, task, branch}` each), and the contract they keep.
    integrate: dict | None = None
    #: The files the change a step reads touches (a review's diff): capabilities about
    #: them join it (`Capability.touches`).
    touches: tuple[str, ...] = ()
    #: A workflow step a blind review of its change waits on: that review reads the
    #: request and the diff as the independent verifier would, so the verifier is not
    #: run twice on the same work (`workflows.reviewed_next`).
    reviewed_next: bool = False
    #: The session's word that the task is one piece (`--whole`): out of turns, it gets more.
    whole: bool = False
    #: What follows a run's ending (filing what it learned, the late independent check) goes on
    #: in a child process, so the command that started it returns at its ending (`locks run`,
    #: `run-queued`). Off, it runs in this process before `run` returns (tests, the bench).
    detach_after: bool = False
    #: A resume's account of where it starts and why (`cli.resume_line`), said in its plan.
    resume_why: str = ""
    #: The loop this task is a round's step of (`locks.loops`): its tools that act are
    #: held by the loop's mode and rules in `locks.mcp_guard`.
    loop: str | None = None
    #: A workflow step a later step forks (`locks.trunks`): its role goes in its brief, never
    #: in its system prompt, which every fork of it would keep.
    trunk_for: bool = False
    #: The trunk this step's first attempt forks (`trunks.source`: its task, step and session),
    #: or why there is none (`missing`).
    trunk: dict | None = None

    def stored(self) -> dict:
        """What a resumed run is started with again, as the task's `options`. A run
        in the checkout itself resumes there: in a worktree it could not see the
        uncommitted work it was continuing."""
        values = {"verify": self.verify, "kind": self.kind,
                  "allow_approval": self.allow_approval, "allow_tools": list(self.allow_tools),
                  "livespec": self.livespec, "no_isolate": not self.isolate, "role": self.role,
                  "step_role": self.step_role, "surfaces": list(self.surfaces),
                  "ledger": self.ledger, "add_dirs": list(self.add_dirs), "services": list(self.services),
                  "run_verify": self.run_verify, "parts": self.parts, "integrate": self.integrate,
                  "touches": list(self.touches), "reviewed_next": self.reviewed_next, "whole": self.whole,
                  # What `flow.queued_options` reads back: a resume stored without them lost them
                  # for every resume after it (a step's start cell, its turns, its base commit).
                  "start": self.start.label if self.start else None, "max_turns": self.max_turns,
                  "base": self.base, "carry": self.carry, "loop": self.loop,
                  "trunk_for": self.trunk_for, "trunk": self.trunk,
                  "max_attempts": self.max_attempts if self.max_attempts != Options.max_attempts else None}
        kept = {k: v for k, v in values.items() if v not in (None, [], False)}
        if not self.keep_learned:
            kept["keep_learned"] = False  # a task whose findings wait on a person's word
        if self.livespec is False:
            kept["livespec"] = False  # a person's --no-livespec, not the setting's default
        return kept


@dataclass
class Plan:
    kind: str
    classification: Classification
    ladder: tuple[Cell, ...]
    start: Cell
    reasons: list[str]
    capabilities: tuple[str, ...]
    #: What adopted neighbours said before the first attempt, for the brief.
    context: tuple[str, ...] = ()
    neighbours: tuple[Status, ...] = ()
    #: The adopted neighbours that are present, with their status.
    active: tuple[tuple[Adapter, Status], ...] = ()
    #: The project's notes the workers are shown.
    notes: tuple[dict, ...] = ()
    #: The role the workers run as, and the team it came from.
    role: roles.Role | None = None
    team: str = ""
    role_why: str = ""
    #: How much the change can hurt, from the request; attempts may raise it.
    risk: risk_mod.Risk = field(default_factory=risk_mod.Risk)
    #: The turns the first attempt starts with when history raised them; None: the kind's own.
    turns: int | None = None
    #: The task's size on the Fibonacci scale, from what the plan knows (`locks.points`).
    points: points_mod.Score = field(default_factory=lambda: points_mod.Score(1))
    #: What it took from the standard of its band (`locks.standards`): None outside a repository.
    standard: dict | None = None
    #: The rung its first try races beside the start (`race_cell`): None when it does not.
    race: Cell | None = None
    #: The arm of a probation trial it starts in (`trust.probation_arm`): None outside one.
    probation: dict | None = None
    #: The candidate notes its brief left out by their trial (`notes.arms`).
    withheld: tuple[int, ...] = ()
    #: What the plan steps before it in its workflow run said could go wrong (`methods.run_risks`),
    #: for its independent check, as where to probe.
    seen_risks: tuple[str, ...] = ()
    #: The order its notes came in when the code and the words found different ones (`notes.recall_arm`).
    recalled: dict | None = None
    #: Lines of the plan this repository already heard today (`said_today`): kept, not shown again.
    quiet: frozenset[str] = frozenset()
    #: How a worker finds its way by each neighbour's index (the neighbour's name, the line): briefed
    #: before the context when that neighbour's tools are handed, left out when they are withheld
    #: (`handing.arm`).
    oriented: tuple[tuple[str, str], ...] = ()
    #: The start raised one rung on this repository's record of the task's shape (`locks.predict`), with
    #: its evidence; None when the start came from the rules alone.
    predicted: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkerSession:
    """The last attempt's Claude Code session, as the next attempt may continue it."""
    #: The session id; "" when none was kept (not in a worktree, or the CLI gave none).
    id: str
    #: What ran it: the model id a person pinned, else the alias.
    model: str
    cell: Cell
    attempt: int


def in_place_files(names: Sequence[str], changed: Sequence[str]) -> dict[str, list[str]]:
    """The changed files of a run with no worktree, grouped by the checkout (one of `names`, the
    path from the folder it started in) that holds them; files in none of them are under the
    folder itself, "(the folder)". Only the checkouts that changed."""
    found: dict[str, list[str]] = {}
    for path in changed:
        owner = next((n for n in sorted(names, key=len, reverse=True) if path.startswith(n + "/")), "(the folder)")
        found.setdefault(owner, []).append(path)
    return dict(sorted(found.items()))


@dataclass
class Report:
    task_id: int | None
    status: str
    plan: Plan
    cells: list[str] = field(default_factory=list)
    #: The model that served each attempt, as the CLI reported it ("" when it did not).
    served: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    #: Each move's full account: where it went, along which dial, and why.
    decisions: list[Decision] = field(default_factory=list)
    final_cell: str | None = None
    cost_usd: float = 0.0
    branch: str | None = None
    summary: str = ""
    impact: list[str] = field(default_factory=list)
    #: What the task changed, from git — never from the worker.
    changed: list[str] = field(default_factory=list)
    #: Files locks' own check wrote in the worktree (`isolate.rewritten`): committed as its output, and said.
    check_output: list[str] = field(default_factory=list)
    #: Whether it worked in a worktree; outside one, its changes are in the checkout.
    isolated: bool = False
    #: The checkouts, by name from the folder the run started in, that a run with no worktree changed in
    #: place: a folder that holds several repositories is no checkout, and has none per repository.
    in_place: list[str] = field(default_factory=list)
    #: What checked the work it left: "locks" (its check, or `--verify`, passed), "worker"
    #: (only the worker's own check, not re-run by locks), or "" (nothing passed).
    checked: str = ""
    #: The commit its kept branch ends on.
    commit: str | None = None
    #: Why it stopped short of a pass, and who made that call.
    stop: stops.Stop | None = None
    #: Earlier copies of this task, same text, taken off the board by its pass.
    superseded: list[int] = field(default_factory=list)
    #: What the passing worker answered for each criterion `S#` of the request: id, met, evidence.
    criteria: tuple[dict[str, Any], ...] = ()
    #: What the passing worker learned, and where each fact was filed in the notes.
    learned: tuple[str, ...] = ()
    noted: list[dict] = field(default_factory=list)
    #: Its independent check runs after this ending (`late_verifier`): a blocker it shows reopens it.
    late_check: bool = False
    #: Rules the passing worker says a failing check of this task showed (`notes.CONSTRAINT`).
    constraints: tuple[str, ...] = ()
    #: A passing plan's parts and the contract they keep, for the workflow step that fans out over it.
    parts: tuple[dict, ...] = ()
    contract: str = ""
    #: Security problems the passing worker named (`alerts`), secrets masked.
    alerts: list[str] = field(default_factory=list)
    #: locks' check failed only as it does at the base (`Baseline.found`): what it found, else None.
    preexisting: dict[str, Any] | None = None
    #: Why the passing worker never ran its own check (out of turns, refused), when locks' passed in its place.
    unchecked: str = ""

    def checked_text(self) -> str:
        """What checked the work, said as it is: "unverified" only when no check locks ran passed."""
        if self.checked == "locks":
            return "checked by locks' own run of its check"
        if self.checked == "worker":
            return "checked by the worker's own check, not re-run by locks"
        return "unverified"

    def text(self) -> str:
        lines = [
            f"task #{self.task_id}: {self.status}" if self.task_id else f"plan: {self.status}",
            f"kind {self.plan.kind} ({self.plan.classification.source}: {self.plan.classification.reason})",
            f"ladder {' → '.join(c.label for c in self.plan.ladder)}",
            f"start {self.plan.start.label}" + "".join(f"\n  · {r}" for r in self.plan.reasons
                                                       if r not in self.plan.quiet),
        ]
        if self.plan.role is not None:
            lines.append(f"role {self.plan.role.name} ({self.plan.role.scope}) of the {self.plan.team} team: "
                         f"{self.plan.role_why}")
        lines.append(f"risk {self.plan.risk.tier}" + (f": {'; '.join(self.plan.risk.reasons)}"
                                                      if self.plan.risk.reasons else ""))
        lines.append(f"points {self.plan.points.line()}")
        lines += [line for status in self.plan.neighbours if (line := status.line()) not in self.plan.quiet]
        if self.plan.quiet:
            lines.append(f"({len(self.plan.quiet)} note(s) this repository already heard today left out: "
                         f"`locks show {self.task_id}` keeps them)")
        if self.task_id is None:
            # A dry run shows what the first worker would be handed.
            lines += [line for _, line in self.plan.oriented]
            lines += self.plan.context
        if self.plan.capabilities:
            lines.append(f"capabilities {', '.join(self.plan.capabilities)}")
        for i, (cell, move) in enumerate(zip(self.cells, [*self.moves, ""], strict=False), 1):
            model = self.served[i - 1] if i <= len(self.served) and self.served[i - 1] else ""
            lines.append(f"attempt {i} {cell}" + (f" [{model}]" if model else "") + (f" → {move}" if move else ""))
            if i <= len(self.decisions):
                d = self.decisions[i - 1]
                lines += [f"    · {line}" for line in d.because[1:]]
                lines += [f"    · skipped {line}" for line in d.skipped]
        if self.final_cell:
            lines.append(f"passed at {self.final_cell}")
        if self.cost_usd:
            lines.append(f"cost ${self.cost_usd:.2f}")
        if self.task_id and self.plan.kind not in READ_ONLY_KINDS:
            more = len(self.changed) - 8
            shown = ", ".join(self.changed[:8]) + (f" and {more} more" if more > 0 else "")
            lines.append(f"changed (from git): {shown or 'nothing'}")
            if self.check_output:
                more = len(self.check_output) - 8
                wrote = ", ".join(self.check_output[:8]) + (f" and {more} more" if more > 0 else "")
                lines.append(f"written by the check itself ({len(self.check_output)}, committed as its output; "
                             f"a check that rewrites tracked files may give other files in your checkout): {wrote}")
        at = f" (commit {self.commit[:12]})" if self.commit else ""
        if self.branch and self.status == "cancelled":
            lines.append(f"branch {self.branch} keeps what it did before the cancel, unverified: "
                         f"`git diff HEAD...{self.branch}` shows it; `git branch -D {self.branch}` drops it")
        elif self.branch and self.status == "done":
            lines.append(f"branch {self.branch}{at} — review it, then merge it; your checkout has none of it until "
                         f"then; {self.checked_text()}")
            if self.late_check:
                lines.append("its independent check runs now, after this ending: a problem it shows reopens the task "
                             "and reaches this session as a new ending")
        elif self.branch:
            then = ("decide the open point, then merge it" if self.stop is not None and self.stop.cause == "decision"
                    else f"or `locks resume {self.task_id}` once the block is cleared")
            lines.append(f"branch {self.branch}{at} keeps this task's work, {self.checked_text()}: review it with "
                         f"`git diff HEAD...{self.branch}`, {then}")
        elif self.task_id and self.changed and not self.isolated:
            # No worktree (asked for, or a folder that is no checkout): there is no
            # branch to review, the files themselves are the work.
            if self.in_place:
                counts = ", ".join(f"{name} ({len(files)} file{'s' if len(files) != 1 else ''})"
                                   for name, files in in_place_files(self.in_place, self.changed).items())
                lines.append(f"these changes are in your checkouts, repository by repository: {counts}; the run had "
                             f"no worktree (it started in a folder that holds several repositories), so none of them "
                             f"has a branch to review or merge; {self.checked_text()}")
            else:
                lines.append(f"these changes are in your checkout (the run had no worktree), {self.checked_text()}")
        elif self.task_id and self.status != "done" and self.changed:
            lines.append("nothing of it was kept")
        if said := preexisting_line(self.preexisting):
            lines.append(said)
        lines += self.impact
        if self.superseded:
            lines.append("off the board: " + ", ".join(f"#{i}" for i in self.superseded)
                         + ", earlier copies of this task that had stopped")
        lines += [f"⚠ security: {alert}" for alert in self.alerts]
        if self.noted:
            lines.append("notes: " + "; ".join(
                f"{'kept' if n['new'] else 'already known as'} #{n['id']} [{n['topic']}] {n['title']}"
                for n in self.noted))
        if self.summary:
            lines.append(self.summary)
        if self.stop is not None and self.task_id:
            lines.append(f"stopped by {stops.WHO.get(self.stop.who, self.stop.who)}: {stops.what_to_do(self.stop)}")
            command = stops.next_step(self.task_id, self.stop)
            if command:
                lines.append(f"  {command}")
            rules = self.stop.allow or allow_rules.from_refusals(self.stop.denied)
            if self.stop.cause == "permission" and rules:
                lines.append("  or, for every task in this repository: locks allow "
                             + " ".join(shlex.quote(r) for r in rules))
        return "\n".join(lines)


#: Reading kinds a high complexity does not raise: their first rung is measured as enough. None now:
#: a plan's first rung is sonnet/high (`bench/speed/plans.py`), and the one it rises to is opus/high,
#: which drew the same parts as opus/xhigh and opus/max (`bench/speed/plan.py`).
UNRAISED_KINDS: frozenset[str] = frozenset()


def choose_start(
    ladder: Sequence[Cell],
    complexity: str,
    repo_landings: Sequence[str],
    global_landings: Sequence[str],
    *,
    writes: bool = False,
    kind: str | None = None,
) -> tuple[Cell, list[str]]:
    """The cell the first attempt runs at, and why.

    History only ever *raises* the start. A kind that always climbs two rungs up
    in this repository starts there and skips two failed attempts; a kind that
    passes where it starts (a landing of "", `Store.landings`) is no reason to
    start higher, wherever it started. A high complexity raises a reading kind's
    start one rung, never a writing kind's: a build classified high passed as often
    at its ladder's first cell, five times sooner (`bench/speed/versus.py`). A plan classified
    high rises from sonnet/high to opus/high (`UNRAISED_KINDS` holds the kinds that would not).
    """
    index, reasons = 0, []
    if complexity == "high" and len(ladder) > 1 and not writes and kind not in UNRAISED_KINDS:
        index = 1
        reasons.append("classified as high complexity: one rung up")
    samples, where = (repo_landings, "this repository") if len(repo_landings) >= MIN_REPO_SAMPLES else (
        global_landings, "all repositories")
    if len(samples) >= (MIN_REPO_SAMPLES if where == "this repository" else MIN_GLOBAL_SAMPLES):
        positions = sorted(_index_of(ladder, Cell.parse(label)) if label else 0 for label in samples)
        learned = positions[(len(positions) - 1) // 2]
        if learned > index:
            index = learned
            reasons.append(f"in {where}, half of the last {len(samples)} tasks of this kind "
                           f"had to climb to {ladder[learned].label} or above")
    if not reasons:
        reasons.append("the ladder's first cell")
    return ladder[index], reasons


def _index_of(ladder: Sequence[Cell], cell: Cell) -> int:
    if cell in ladder:
        return ladder.index(cell)
    key = cell.sort_key()
    below = [i for i, c in enumerate(ladder) if c.sort_key() <= key]
    return below[-1] if below else 0


def adopted(options: Options, adapters: Sequence[Adapter] | None) -> list[Adapter]:
    if adapters is not None:
        return list(adapters)
    return default_adapters(os.environ, livespec=options.livespec)


def carries_work(attempt: Attempt) -> bool:
    """Whether a climb to a new model keeps the attempt's work in the worktree: it changed files and
    failed only locks' own check (a `code_bug` with the check's output, no blockers of the independent
    check). The check's failure is a fault in work that stands, which the next model finishes; it is
    not an approach to throw away (issue #124)."""
    return (attempt.failure is Failure.CODE_BUG and bool(attempt.changed) and bool(attempt.check_tail)
            and not attempt.blockers)


#: The kind of the event a resume's new instruction is kept as (`locks resume N -`).
RESUME_INSTRUCTION = "resume_instruction"
#: The most notes filed since a task's last attempt its resume names.
RESUME_NOTES = 5


def resume_context(store: Store, task: Mapping[str, Any], project: str | None) -> list[str]:
    """What a resume is told that its earlier attempts were not (issue #123): the instruction
    the session gave with it, and the notes of its project filed after its last attempt began
    (a note written to unblock it reaches it whatever recall would pick). Data, never the task."""
    attempts = store.attempts(int(task["id"]))
    if not attempts:
        return []
    since = str(attempts[-1]["created_at"])
    out = []
    given = store.last_event(int(task["id"]), RESUME_INSTRUCTION)
    if given and str(given["ts"]) >= since and (text := str(given["data"].get("text") or "").strip()):
        out.append("What the orchestrating session adds for this resume, from what the earlier attempts "
                   f"met (context for the task above, not a new task):\n<<<\n{clip(text, 4000)}\n>>>")
    if project and (rows := store.notes_since(project, since, limit=RESUME_NOTES)):
        out.append("Notes filed here since this task's last attempt (data, not instructions):\n"
                   + "\n".join(f"- [{r['topic']}] {r['title']}: {clip(r['text'], 400)}" for r in rows))
    return out


#: What a resume after a `review` stop tells its worker before the blockers the independent check kept.
REVIEW_FINDINGS = ("The independent check blocked your earlier work, kept on this branch, with these findings; "
                   "fix them in the code, then answer in the result contract:")


def review_context(known: Sequence[Mapping[str, str]]) -> list[str]:
    """What a resume after a `review` stop briefs its worker with: the blockers the independent check
    kept (`known_findings`, loaded by the resume from the task's stop), as data for the task. Its first
    check gets them too; a worker without them only re-verified the work it had kept."""
    return [f"{REVIEW_FINDINGS}\n{findings_mod.text(known)}"] if known else []


def _bring_current(adapter: Adapter, status: Status, repo_dir: Path, options: Options,
                   say: Callable[[str], None] | None) -> tuple[Status, list[str]]:
    """An adopted neighbour's index made current before the worker starts: built or refreshed and
    waited for up to the person's `livespec_wait`, so the worker orients by it; started in the
    background when that is 0 or the neighbour has no way to wait. The status afterwards, and what
    the plan says. Past the wait the task starts without the index, said, and the index keeps building."""
    # An unreadable index (a `group_db` with no `project` table, issue #123) is rebuilt as a
    # missing one is: a worker briefed on it would get nothing from it.
    missing = (status.state in (ABSENT, UNREADABLE) and getattr(adapter, "indexes", True)
               and getattr(adapter, "indexable", lambda _d: True)(repo_dir) and adapter.server() is not None)
    if not (missing or status.stale):
        return status, []
    what = "index" if missing else "refresh"
    meanwhile = "this task runs without it" if missing else "this task uses the index as it is"
    failed = getattr(adapter, "last_failure", lambda _d: None)(repo_dir)
    if failed:
        # Said once: a failure every plan repeats until the checkout moves is noise.
        if options.dry_run or not getattr(adapter, "failure_said", lambda _d: False)(repo_dir):
            return status, [f"{adapter.name}: its last index here failed ({clip(failed, 160)}); not "
                            f"tried again until the checkout moves; {meanwhile}"]
        return status, []
    if adapter.server() is None:
        return status, [f"{adapter.name}: cannot {what} here; {meanwhile}"]
    wait_s = config.livespec_wait(os.environ)
    ensure = getattr(adapter, "ensure_fresh", None) if wait_s > 0 else None
    if options.dry_run:
        where = f"before the worker starts (waits up to {wait_s} s)" if ensure else "in the background"
        return status, [f"{adapter.name}: would {what} {where}"]
    if ensure is not None:
        done = ensure(repo_dir, wait_s, say=say)
        return adapter.inspect(repo_dir), [line for found in done if (line := found.line())]
    if adapter.refresh_in_background(repo_dir, home() / f"{adapter.name}-index.log"):
        return status, [f"{adapter.name}: {what}ing in the background; {meanwhile}"]
    return status, [f"{adapter.name}: already {what}ing in the background; {meanwhile}"]


#: Kinds whose worker reads and answers: one given a request that orders a change cannot do it.
_READING_KINDS = frozenset({"chat", "explore", "review-routine", "review-critical"})


def _caller_kind(options: Options, text: str) -> Classification:
    """The kind a caller passed with `--kind`. A reading kind given a request that opens with a verb of
    change ("Implement the export…") is widened to `implement`, said in the plan: its worker could only
    stop on the change it was refused. A tag in the text is the person's word and keeps it; so does a
    request that says it changes nothing, a question, and a workflow step, whose definition chose it."""
    given = Classification(str(options.kind), "medium", "rule", "chosen by the caller")
    if (given.kind not in _READING_KINDS or options.step_role or options.ledger is not None or options.touches
            or not opens_with_change(text) or text.rstrip().endswith("?")
            or declares_read_only(given.kind, text)
            or ((tagged := by_rules(text)) is not None and tagged.reason.startswith("tagged"))):
        return given
    return Classification("implement", "medium", "rule",
                          f"--kind {given.kind} widened to implement: the request opens with a change, "
                          "and a reading worker cannot make one")


def plan(text: str, repo_dir: Path, store: Store, options: Options, registry: Mapping[str, caps.Capability],
         *, classifier: Callable[[str], Classification] | None = None,
         adapters: Sequence[Adapter] | None = None, say: Callable[[str], None] | None = None) -> Plan:
    catalog = roles.load()
    team, members, team_notes = _team(options, repo.key(repo_dir), catalog)
    covered = roles.kinds_of(members)

    def classified() -> Classification:
        if options.kind:
            return narrow(_caller_kind(options, text), covered)
        if classifier is not None:
            return narrow(classifier(text), covered)
        return classify(text, use_model=options.use_model_classifier, cwd=home(), kinds=covered)

    # The kind and the neighbours' indexes are found side by side: neither needs the other, and each
    # waits on a process (Haiku's answer, ~4 s; an index built or refreshed, up to `livespec_wait`).
    # A real plan took 13.7 s doing them one after the other.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(classified)
        brought = [(adapter, *_bring_current(adapter, adapter.inspect(repo_dir), repo_dir, options, say))
                   for adapter in adopted(options, adapters)]
        classification = pending.result()
    kind = classification.kind

    neighbour_notes: list[str] = []
    statuses: list[Status] = []
    active: list[tuple[Adapter, Status]] = []
    context: list[str] = []
    oriented: list[tuple[str, str]] = []
    raise_rungs, critical = 0, False
    # The symbols the request reaches, when an index could say: None when none could.
    reached: int | None = None
    for adapter, status, index_notes in brought:
        neighbour_notes += index_notes
        statuses.append(status)
        if not status.present:
            continue
        active.append((adapter, status))
        # How to find its way by this neighbour's index comes before the map the index drew.
        orient = getattr(adapter, "orient", lambda _kind, _status: ())
        oriented += [(adapter.name, line) for line in orient(kind, status)]
        briefing = adapter.brief(text, repo_dir, status)
        context += briefing.lines
        raise_rungs = max(raise_rungs, briefing.raise_rungs)
        critical = critical or briefing.critical
        if briefing.reached is not None:
            reached = max(reached or 0, briefing.reached)
        neighbour_notes += briefing.reasons

    if critical and kind == "review-routine" and (not covered or "review-critical" in covered):
        kind = "review-critical"
        neighbour_notes.append("the code under review implements a critical spec: reviewed as critical")
    role, why = roles.pick(kind, members, repo_dir)
    neighbour_notes += team_notes
    risk = risk_mod.of_request(text, kind)
    size = points_mod.of_request(text, kind, risk, surfaces=options.surfaces, reached=reached)
    size = points_mod.corrected(size, kind, *standards.size_ratio(store, repo.key(repo_dir), kind))
    if role is not None:
        missing = [s for s in (*role.skills, *role.on_demand) if roles.find_skill(s) is None]
        if missing:
            neighbour_notes.append(f"role {role.name}: skill(s) {', '.join(missing)} not found; put each in "
                                   f"{home() / 'skills'}/<name>/SKILL.md or install it for Claude Code")
        absent = [c for c in role.capabilities if c not in registry and c not in {a.name for a, _ in active}]
        if absent:
            neighbour_notes.append(f"role {role.name}: capability {', '.join(absent)} is not in your registry "
                                   "or an adopted neighbour here; the workers run without it")
    if len(text) >= points_mod.LONG_CHARS and size.points > points_mod.on_scale(0):
        # Said so a session can trim it: length alone moves a request up the scale (issue #118).
        neighbour_notes.append(f"its length ({len(text):,} characters) adds to its size ({size.points} points), "
                               "and the size can raise where it starts: a shorter request may start lower")
    ladder = ladder_for(kind)
    key = repo.key(repo_dir)
    # The standard of the task's band here: where tasks of its kind and size start, as measured.
    standard = standards.for_plan(store, key, kind, size.points, pinned=options.start is not None,
                                  explicit_turns=bool(options.max_turns)) if key else None
    if options.start is not None:
        start, reasons = options.start, ["pinned by the caller"]
    else:
        if standard is not None and standard.cell is not None:
            start, reasons = standard.cell, []
        else:
            start, reasons = choose_start(
                ladder, classification.complexity,
                # A writing kind's pass is checked, so a pass where it started says the cells
                # below were never shown to fail; a reading kind's is its answer, kept as before.
                store.landings(kind, repo=key, climbs=kind not in READ_ONLY_KINDS) if key else [],
                store.landings(kind, climbs=kind not in READ_ONLY_KINDS),
                writes=kind not in READ_ONLY_KINDS, kind=kind,
            )
        if raise_rungs:
            raised = min(_index_of(ladder, start) + raise_rungs, len(ladder) - 1)
            if ladder[raised] != start:
                start = ladder[raised]
                reasons.append(f"one rung up for what the code index found ({start.label})")
    if standard is not None:
        reasons += standard.reasons
    turns = None
    if not options.max_turns and standard is not None and standard.turns:
        turns = standard.turns
    elif not options.max_turns and key:
        raised_turns = turns_from_history(store.turn_history(kind, repo=key, limit=TURN_HISTORY), kind,
                                          large=size.points >= LARGE_POINTS)
        if raised_turns:
            turns, why = raised_turns
            reasons.append(why)
    habits.load_events(store)
    learned = habits.recipes(store, kind, repo=repo.key(repo_dir))
    if learned:
        context.append("Steps that came before passing attempts on this kind of task here: "
                       + "; ".join(learned) + ". Use them unless the task says otherwise.")
    if kind not in READ_ONLY_KINDS and (flaky := known_flaky(store, repo_dir)):
        # Every worker met them on its own; what locks saw is said once, to all of them.
        context.append("Tests known to flake in this repository (locks saw each fail and then pass when the "
                       "same check ran again): " + "; ".join(flaky) + ". One of them failing alone says nothing "
                       "about your change: run it again before changing code for it, and name it in `summary`.")
    check = options.verify or options.run_verify
    if kind not in READ_ONLY_KINDS and (unmeasured := _unmeasured(kind, touched_paths(text, options), registry)):
        neighbour_notes.append(unmeasured)
    neighbour_notes += _unmet(text, registry, served=bool(options.services), verify=check, where=repo_dir)
    if check and kind not in READ_ONLY_KINDS:
        neighbour_notes += _beside_ci(repo_dir, check, options.services, ci.quiet(store, repo.key(repo_dir)))
    if kind not in READ_ONLY_KINDS and (several := readmes(text, repo_dir)):
        reasons.append(several)
    if check and flake_fix_runs(kind, text) > 1:
        reasons.append(f"this work fixes a flaky test: `{check}` runs {FLAKE_FIX_RUNS} times and must pass "
                       "every time, since one pass says nothing of a flake")
    if check and (weak := ci.weak(check, repo_dir)):
        neighbour_notes.append(weak)
    if check and (bypass := ci.bypassed(check, repo_dir)):
        neighbour_notes.append(bypass)
    if spans := _spans(repo_dir, options.add_dirs):
        neighbour_notes.append(spans)
    if not options.base and kind not in READ_ONLY_KINDS and (old := isolate.behind_trunk(repo_dir)):
        reasons.append(old)
    for lag in models.lagging(store):
        neighbour_notes.append(f"workers last ran {lag['workers']} for `{lag['alias']}` while your sessions use "
                               f"{lag['sessions']}: the installed Claude Code's alias is older. Pin it with "
                               f"`{lag['command']}`, or update Claude Code")
    mode = grants.mode_for(start.model)
    seen = _refused_here(store, repo.key(repo_dir), (*options.allow_tools, *grants.granted(repo.key(repo_dir))),
                         mode)
    if mode != "auto":
        neighbour_notes.append(f"workers on {start.model} run in {mode} mode")
    node = runtime.node_bin(repo_dir)
    if node is not None:
        neighbour_notes.append(f"workers run node {node.version} from {node.bin} ({node.source})")
    elif runtime.wanted(repo_dir) is None and (unmet := runtime.needed(repo_dir)[1]):
        neighbour_notes.append(unmet)
    if seen:
        neighbour_notes.append("workers here were refused before: " + ", ".join(f"{r} ×{n}" for r, n in seen)
                               + ". Pass the ones this task needs with --allow, or a worker stops on them")
    expected = store.expected_cost(kind, repo=repo.key(repo_dir)) or store.expected_cost(kind)
    if expected:
        neighbour_notes.append(f"finished {kind} tasks cost ${expected[0]:.2f} on average (last {expected[1]})")
    known: list[dict] = []
    withheld: list[dict] = []
    recalled: dict[str, str] = {}
    key = repo.key(repo_dir)
    if key and config.enabled("notes", os.environ, options.notes):
        if not options.dry_run:
            notes.review(store, key, repo_dir)
        # A note written on work not merged where the task starts is said to be so; the
        # project's constraints come apart, as requirements, every one that fits.
        # By the code it touches (the notes anchored to the files it names, its surfaces, a step's
        # diff) and by its words, strictly: one word in common puts an unrelated note in its brief.
        touched = touched_paths(text, options)
        found = notes.recall(store, key, text, paths=touched, limit=6, strict=True)
        mixed = notes.recall(store, key, text, paths=touched, limit=6, strict=True, fuse=True) \
            if notes.recall_on() else found
        if [n["id"] for n in mixed] != [n["id"] for n in found]:
            # The code and the words found different notes: which order the brief takes is on trial.
            side, side_why = notes.recall_arm(store, key, int(hashlib.sha256(text.encode()).hexdigest()[:8], 16))
            recalled = {notes.RECALL_KEY: side, "recall_why": side_why}
            found = mixed if side == "fused" else found
        known = notes.unmerged([n for n in found if n.get("kind") != notes.CONSTRAINT], repo_dir, options.base)
        # A worker's candidate note goes without in turn, so it is weighed against its absence (`notes.arms`).
        known, withheld = notes.arms(store, known)
        if withheld:
            neighbour_notes.append(f"notes: {len(withheld)} candidate(s) left out on trial ("
                                   + ", ".join(f"#{n['id']}" for n in withheld) + ")")
        if known:
            context += notes.brief_lines(known)
            neighbour_notes.append(f"notes: {len(known)} from this project's memory ("
                                   + ", ".join(f"#{n['id']} {n['topic']}" for n in known) + ")")
        kept_rules = notes.constraints(store, key)
        if kept_rules:
            context += notes.constraint_lines(kept_rules)
            neighbour_notes.append(f"constraints: {len(kept_rules)} this project keeps")
            known = [*known, *kept_rules]
    # A repository that follows TRACER (`docs/tracer/`): its writing workers read the method first.
    if kind not in READ_ONLY_KINDS and config.enabled("tracer", os.environ) and (
            method := tracer_mod.brief_lines(repo_dir, text)):
        context += method
        neighbour_notes.append(f"tracer: this repository follows TRACER; its audience, floors, anti-patterns "
                               f"and {len(tracer_mod.flows_for(repo_dir, text))} named flow(s) go in the brief")
    # A repository that keeps OpenSpec specs: its writing workers keep the ones they reach.
    if kind not in READ_ONLY_KINDS and config.enabled("specs", os.environ) and (
            drifted := specs_mod.drift(repo_dir, options.base or "HEAD")):
        first = kind in specs_mod.FIRST_KINDS and specs_mod.first(store, key)
        context += specs_mod.brief_lines(drifted, first=first)
        for adapter, _ in active:
            # Where the index can read them, a spec the work writes is linked to its code (`@spec:` comments).
            context += getattr(adapter, "spec_links_brief", lambda _dir: [])(repo_dir)
        if first:
            neighbour_notes.append("spec-first: this repository accepts no build without its spec; a pass that "
                                   "writes none goes back to its worker once, then to the orchestrator")
        if drifted.behind:
            neighbour_notes.append(f"specs: {drifted.text()}; the worker is told to keep the ones it reaches")
    if kind not in READ_ONLY_KINDS and (named := [p for p in touched_paths(text, options) if "*" not in p]):
        # What the code index says the files the request names reach, asked by locks before the worker
        # starts (data), so the worker knows it whether or not it calls the index itself.
        for adapter, status in active:
            try:
                found = list(getattr(adapter, "reach", lambda _p, _r, _s: ())(named, repo_dir, status))
            except Exception:  # an index that cannot say costs the plan nothing
                found = []
            if found:
                context.append("What the code index says the files this request names reach, before any change "
                               "(data; probe what matters):\n" + "\n".join(f"- {line}" for line in found))
                neighbour_notes.append(f"{adapter.name}: what {len(named)} named file(s) reach is in the brief")
    selected = _with_role(caps.select(registry, kind, workdir=repo_dir, paths=touched_paths(text, options),
                                      reading=kind in READ_ONLY_KINDS), role, registry, repo_dir)
    names = tuple(dict.fromkeys([*selected.names, *(a.name for a, _ in active)]))
    # A cheaper cell below the ladder, on trial where the first rung already passes here
    # (`trust.probation_arm`); one a standard made the start (`standards.rungs_of`) climbs into the ladder.
    arm, arm_why = trust.probation_arm(
        store, key, kind, size.points, start, tier=risk.tier,
        eligible=(options.start is None and not raise_rungs and not options.resume_why and options.isolate
                  and not options.launch_dir and not options.ledger and not options.integrate and not options.parts))
    if arm == "probation":
        start = PROBATION[kind]
    if arm_why:
        reasons.append(arm_why)
    if start == PROBATION.get(kind):
        ladder = reach(kind)
    # A start one rung up for a task shaped like ones that failed their first try here, where the
    # rung above rescued them (`locks.predict`): a raise read from the record, never a lower start.
    predicted = None
    if options.start is None and arm != "probation" and key and config.enabled("predict", os.environ):
        found = predict_mod.shape(kind, size.points, risk.tier, len(risk_mod.areas(risk)))
        if (got := predict_mod.start(store, key, kind, found, start, ladder)) is not None:
            start, predicted = got
            reasons.append(predict_mod.line(predicted))
    race, race_why = race_cell(store, key, kind, ladder, start, options)
    if race_why:
        reasons.append(race_why)
    return Plan(kind, classification, ladder, start, reasons + neighbour_notes, names,
                tuple(context), tuple(statuses), tuple(active), tuple(known), role, team, why,
                risk, turns, size, standard.data() if standard is not None else None, race,
                probation={"arm": arm, "cell": PROBATION[kind].label} if arm else None,
                withheld=tuple(int(n["id"]) for n in withheld), recalled=recalled or None,
                oriented=tuple(oriented), predicted=predicted)


#: A start whose first tries passed less often than this here, over at least `RACE_SAMPLES`
#: of them, races the next rung on its first try (`race_cell`).
RACE_BELOW = 0.5
RACE_SAMPLES = 4


def race_cell(store: Store, key: str | None, kind: str, ladder: Sequence[Cell], start: Cell,
              options: Options) -> tuple[Cell | None, str]:
    """The rung a task's first try races beside its start, and why; (None, "") when it does
    not race. A climb after a first try that failed is a whole attempt more of waiting: where
    the start's first tries pass less than `RACE_BELOW` of the time in this repository, the
    next rung that needs no approval runs at the same time, each in its own worktree, and
    the first that passes its check is kept (the other is stopped). Money is no brake, so the
    second worker is the price of the time. Never a pinned start, a reading kind, a step that
    decides a ledger, joins parts or plans them, work in the person's checkout, or with
    `race` off. Racing judges itself (`racing.judge`): where its record says it rarely
    paid here, the start does not race, the climb goes deep instead, and the reason says
    so beside no rung."""
    if (not key or options.start is not None or kind in READ_ONLY_KINDS or not options.isolate
            or options.launch_dir or options.ledger or options.integrate or options.parts
            or not config.enabled("race", os.environ)):
        return None, ""
    after = list(ladder)[_index_of(ladder, start) + 1:]
    rung = next((c for c in after if not c.needs_approval), None)
    if rung is None:
        return None, ""
    mine = [t for t in standards.ended(store, repo=key) if t.kind == kind]
    window = mine[-standards.WINDOW:]
    passed, tried = standards.rates(window).get(start.label, [0, 0])
    if tried < RACE_SAMPLES or passed / tried >= RACE_BELOW:
        return None, ""
    outgrew, failed = _outgrew(window, start.label)
    if failed and outgrew * 2 >= failed:
        # Out of turns is room, never the cell: a rung beside it outgrows the same turns.
        return None, (f"its first try does not race {start.label} against {rung.label}, though {passed} of {tried} "
                      f"first tries at {start.label} passed here: {outgrew} of the {failed} that failed ran out of "
                      "turns, which another cell does not fix; work this size passes in smaller tasks or with more "
                      "turns")
    record = racing.judge(window, mine)
    if not record.pays:
        return None, (f"its first try does not race {start.label} against {rung.label}, though {passed} of {tried} "
                      f"first tries at {start.label} passed here: {record.why}")
    return rung, (f"its first try races {start.label} against {rung.label}: {passed} of {tried} first tries at "
                  f"{start.label} passed here; the first to pass its check is kept, the other stopped"
                  + (f" ({record.why})" if record.races else ""))


def _outgrew(tasks: Sequence[standards.Ended], label: str) -> tuple[int, int]:
    """Of the tasks whose first try ran on `label` and failed, how many ran out of turns on it,
    and how many failed: a race gives the try another cell, never more room."""
    outgrew = failed = 0
    for task in tasks:
        first = standards.samples(task.attempts) if not task.pinned else []
        if not first or first[0][0] != label or first[0][1]:
            continue
        failed += 1
        outgrew += any(a.get("failure") == Failure.TURNS_EXHAUSTED.value and a.get("cell") == label
                       for a in task.attempts)
    return outgrew, failed


@dataclass
class Raced:
    """What a race's first try came to: the try that goes on (the one that passed, else the
    higher rung's, unless only the environment stopped it) and the one recorded beside it."""

    result: launch.WorkerResult
    cell: Cell
    spec: launch.LaunchSpec
    other: launch.WorkerResult
    other_cell: Cell
    #: The kept try's check already ran, in the worktree its work came from.
    checked: bool


def _skips(store: Store, key: str | None, kind: str) -> dict[tuple[str, str], Skip]:
    """The standards' skips in force for this kind here, as `escalate.decide` takes them."""
    return {pair: Skip(s["onto"], s["change"], s["because"])
            for pair, s in standards.active_skips(store, key, kind).items()}


#: The move a race's stopped try is kept with: it says nothing of its cell (`standards.samples`).
RACED_OUT = racing.RACED_OUT


def _record_raced(store: Store, task_id: int, raced: Raced, turns: int, capabilities: Sequence[str],
                  report: Report, *, first: bool) -> int:
    """Keep the try a race did not go on with, as an attempt of its own: its cost, its cell
    and what it said. When it is the start's (`first`), the rung's that goes on follows it as
    a new start (`race`); a try stopped because the other passed first teaches no cell."""
    o = raced.other
    stopped = o.summary.startswith("stopped:")
    seq = store.add_attempt(
        task_id, cell=raced.other_cell.label, max_turns=turns, passed=0,
        failure=(o.failure or Failure.INCONCLUSIVE).value, summary=o.summary, evidence=kept_evidence(o.evidence),
        cost_usd=o.cost_usd, input_tokens=o.input_tokens, output_tokens=o.output_tokens, turns=o.turns,
        duration_s=o.duration_s, changed_paths=o.changed_paths, served_model=o.served_model or None,
        capabilities=capabilities)
    store.add_event(task_id, "attempt_finished", seq=seq, cell=raced.other_cell.label, passed=False,
                    failure=(o.failure or Failure.INCONCLUSIVE).value, cost_usd=o.cost_usd, turns=o.turns,
                    summary=o.summary[:500], raced=True, stopped=stopped, wall_s=round(o.duration_s, 2),
                    input_tokens=o.input_tokens, output_tokens=o.output_tokens,
                    cache_read_tokens=o.cache_read_tokens, cache_write_tokens=o.cache_write_tokens,
                    worker_cost_usd=round(o.cost_usd, 4), served_model=o.served_model,
                    **({"check_tail": o.check_tail} if o.check_tail else {}))
    if stopped:
        store.set_move(task_id, seq, RACED_OUT, "the other try passed its check first")
    elif first:
        store.set_move(task_id, seq, "race", f"raced {raced.cell.label} beside it")
    else:
        # Neither passed and the start's went on: the start's row takes the next move, so the race
        # is marked here, where nothing overwrites it (`racing.outcome`, `standards.samples`).
        store.set_move(task_id, seq, racing.RACED_BESIDE, f"raced beside {raced.cell.label}, which went on")
    report.cells.append(raced.other_cell.label)
    report.served.append(o.served_model)
    report.cost_usd += o.cost_usd
    return seq


def _raced_attempt(raced: Raced, turns: int) -> Attempt:
    o = raced.other
    return Attempt(raced.other_cell, turns, False, o.failure or Failure.INCONCLUSIVE, o.summary, o.denied,
                   allow=o.allow, changed=o.changed_paths, evidence=o.evidence[-1500:], transient=o.transient,
                   findings=o.findings, failing=o.failing, base_unknown=o.base_unknown,
                   check_tail=o.check_tail, raced=o.summary.startswith("stopped:"))


def _launch(launcher: Launcher, spec: launch.LaunchSpec, runner: launch.Stoppable) -> launch.WorkerResult:
    if launcher is launch.run:
        return launch.run(spec, runner=runner)
    return launcher(spec)


def _race(launcher: Launcher, spec: launch.LaunchSpec, start: Cell, rung: Cell, workspace: isolate.Workspace,
          verify: str | None, baseline: Baseline | None = None) -> Raced | None:
    """Run the start and the rung side by side, each in its own worktree, each checked in it:
    the first that passes is kept and the other stopped. The task's worktree ends holding the
    kept try's work. None when the second worktree cannot be made: the try runs alone."""
    try:
        lane = isolate.race_lane(workspace, home())
    except (isolate.IsolationError, OSError):
        return None
    there = str(lane.path)
    second = dataclasses.replace(
        spec, cell=rung, model_id=models.pinned(rung.model), permission_mode=grants.mode_for(rung.model),
        launch_dir=lane.path, prompt=spec.prompt.replace(str(workspace.path), there),
        env={**spec.env, "LOCKS_WORKER_ATTEMPT": spec.env.get("LOCKS_WORKER_ATTEMPT", "") + "-race"},
        resume_session="", fork_session=False)
    runners = (launch.Stoppable(), launch.Stoppable())

    def side(i: int, run_spec: launch.LaunchSpec, where: Path) -> launch.WorkerResult:
        found = _launch(launcher, run_spec, runners[i])
        if found.passed and verify and not runners[i].stopped:
            found = _verified(found, verify, where, baseline)
        return found

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(side, 0, spec, workspace.path): 0, pool.submit(side, 1, second, lane.path): 1}
            done: dict[int, launch.WorkerResult] = {}
            pending = set(futures)
            winner = None
            while pending and winner is None:
                finished, pending = wait_for(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    done[futures[future]] = future.result()
                    if done[futures[future]].passed and winner is None:
                        winner = futures[future]
                if winner is not None:
                    for i in (0, 1):
                        if i != winner:
                            runners[i].stop()
            for future in pending:
                done[futures[future]] = future.result()
        if winner is None:
            # Neither passed: the climb goes on from the higher rung, unless only the environment stopped it.
            winner = 0 if done[1].failure is Failure.ENVIRONMENT and done[0].failure is not Failure.ENVIRONMENT else 1
        loser = 1 - winner
        lost = done[loser]
        if done[winner].passed:
            lost = dataclasses.replace(lost, passed=False, summary=clip(
                f"stopped: {(start, rung)[winner].label} passed its check first. {lost.summary}", 2000))
        kept = done[winner]
        if winner == 1:
            isolate.adopt(lane, workspace)
            # Its session lived in the race's worktree, which goes: what follows starts fresh.
            kept = dataclasses.replace(kept, session_id="")
        return Raced(kept, (start, rung)[winner], (spec, second)[winner], lost, (start, rung)[loser],
                     checked=bool(verify) and kept.passed)
    finally:
        isolate.drop(lane)


def touched_paths(text: str, options: Options, changed: Sequence[str] = ()) -> list[str]:
    """What a task's work touches, for the capabilities about those files: the files and
    folders its request names, its surfaces, the diff a workflow step reads, and what its
    work changed so far."""
    return list(dict.fromkeys([*points_mod.named(text, options.surfaces), *options.touches, *changed]))


def turns_from_history(runs: Sequence[Sequence[tuple[int, bool, str | None]]], kind: str, *,
                       large: bool = False) -> tuple[int, str] | None:
    """The turns a task of `kind` starts with when the recent ones here outgrew the kind's
    own and passed with more (each run its attempts' `(max_turns, passed, failure)`): the
    median of what those passes needed, capped. Only ever above the kind's own; None
    when fewer than `MIN_TURN_SAMPLES` needed more (one, for a task of `LARGE_POINTS` or more)."""
    own = turns_for(kind)
    needed = []
    for attempts in runs:
        passes = [turns for turns, passed, _ in attempts if passed]
        if passes and passes[-1] > own and any(
                failure == Failure.TURNS_EXHAUSTED.value and turns <= own for turns, _, failure in attempts):
            needed.append(passes[-1])
    if len(needed) < (1 if large else MIN_TURN_SAMPLES):
        return None
    turns = min(int(statistics.median_low(needed)), MAX_TURNS_CEILING)
    return turns, (f"starts with {turns} turns: {len(needed)} of the last {len(runs)} {kind} tasks here "
                   f"needed more than {own}")


#: How the plan's reasons a person should hear before the work starts begin: what
#: the task needs and workers lack, and what stopped workers here before.
WARNINGS = ("this task needs", "this task mentions", "this task spans", "workers here were refused before", "task #",
            "CI also runs", "this task's check", "base: ")


def warnings(reasons: Sequence[str]) -> list[str]:
    return [r for r in reasons if r.startswith(WARNINGS)]


#: Plan lines that read the same task after task in a repository: said once a day there.
_REPEATED = ("CI also runs", "CI: ", "engram: ", "layout-inspector: ", "livespec: ", "specs: ", "tracer: ",
             "this task spans ", "base: ", manifests.PREFIX)
SAID_META = "said:"


def repeats(line: str) -> bool:
    """Whether a plan line is one a repository hears the same on every task: a neighbour's
    state, the CI jobs a check leaves out, a role's capability that is not there."""
    return line.startswith(_REPEATED) or " is not in your registry " in line


def said_today(store: Store, key: str | None, lines: Sequence[str], *, today: str | None = None) -> frozenset[str]:
    """The lines (`repeats`) this repository already heard today, and the rest marked as heard:
    each is said on the day's first task there and kept on every `planned` event."""
    day = today or datetime.now().astimezone().date().isoformat()
    heard = set()
    for line in dict.fromkeys(x for x in lines if repeats(x)):
        meta = SAID_META + hashlib.sha256(f"{key}\0{line}".encode()).hexdigest()[:32]
        try:
            if store.meta(meta) == day:
                heard.add(line)
            else:
                store.set_meta(meta, day)
        except sqlite3.Error:
            continue
    return frozenset(heard)


def foresee(text: str, repo_dir: Path, store: Store, options: Options) -> list[str]:
    """What a task's plan will warn of when it starts, said before it does: a dry
    plan, classified by rules alone and with no neighbour asked, since a neighbour's
    notes warn of nothing."""
    dry = dataclasses.replace(options, dry_run=True, use_model_classifier=False)
    registry = caps.load_all(home() / "capabilities.json")
    return warnings(plan(text, repo_dir.resolve(), store, dry, registry, adapters=()).reasons)


def _team(options: Options, key: str | None, catalog: roles.Catalog) -> tuple[str, list[roles.Role], list[str]]:
    """The roles a task here may run as: the one a person named, or the project's team."""
    lines: list[str] = []
    if options.role:
        try:
            if options.role in roles.INTERNAL and options.role not in catalog.roles:
                return "locks' own", [roles.internal(options.role, catalog)], []
            return "named for this run", [catalog.get(options.role)], []
        except roles.RoleError as exc:
            # A queued or resumed task whose role is gone since: it runs, as its team.
            lines.append(f"{exc}: the project's team serves")
    team, why = teams.of(key, catalog)
    members, missing = team.members(catalog)
    lines += [] if team.name == teams.BUNDLED else [why]
    if options.step_role:
        named = [r for r in members if r.name == options.step_role]
        if named:
            return team.name, named, [*lines, f"the workflow step runs as {options.step_role}, of this team"]
        lines.append(f"the workflow step names {options.step_role}, which is not in the {team.name} team: "
                     "the team picks the role")
    if missing:
        lines.append(f"the {team.name} team names role(s) that are not there: {', '.join(missing)}")
    if catalog.refused:
        lines.append("roles refused: " + "; ".join(f"{n} ({why})" for n, why in sorted(catalog.refused.items())))
    if not members:
        # A team whose roles are all gone limits nothing: the bundled one serves.
        bundled = teams.bundled(catalog)
        lines.append(f"the {team.name} team has no role left: the {bundled.name} team serves")
        return bundled.name, bundled.members(catalog)[0], lines
    return team.name, members, lines


def _with_role(selection: caps.Selection, role: roles.Role | None, registry: Mapping[str, caps.Capability],
               workdir: Path) -> caps.Selection:
    """The capabilities the role names, from the person's registry, beside the ones
    the kind gets. A role names capabilities; it never brings one."""
    if role is None:
        return selection
    wanted = {name: registry[name] for name in role.capabilities if name in registry
              and name not in selection.servers}
    if not wanted:
        return selection
    extra = caps.select(wanted, "", workdir=workdir, every=True)
    return caps.Selection({**selection.servers, **extra.servers}, (*selection.hints, *extra.hints),
                          (*selection.names, *extra.names), (*selection.read_dirs, *extra.read_dirs))


def run(
    text: str,
    repo_dir: Path,
    store: Store,
    options: Options | None = None,
    *,
    registry: Mapping[str, caps.Capability] | None = None,
    launcher: Launcher | None = None,
    classifier: Callable[[str], Classification] | None = None,
    adapters: Sequence[Adapter] | None = None,
    task_id: int | None = None,
    session_id: str | None = None,
    history: Sequence[Attempt] = (),
    notes_ask: notes.Ask | None = None,
    progress: Callable[[str], None] | None = None,
    known_findings: Sequence[Mapping[str, str]] = (),
) -> Report:
    """Run a task. `history` is a resumed task's earlier attempts: shown to its
    workers, never counted by the escalation, which starts over. `known_findings`
    are what the independent check found before a resume: its first check says for
    each whether it is fixed."""
    options = options or Options()
    # Wall time of the run and of its phases, kept on its events: where a task's
    # time goes (waiting, setting up, working, checking) is evidence for the router.
    clock, began = time.monotonic(), time.time()
    registry = registry if registry is not None else caps.load_all(home() / "capabilities.json")
    launcher = launcher or launch.run
    repo_dir = repo_dir.resolve()
    the_plan = plan(text, repo_dir, store, options, registry, classifier=classifier, adapters=adapters,
                    say=progress)
    plan_s = time.monotonic() - clock
    if options.dry_run:
        if options.verify:
            # What would stop the check from starting is said before the run, not at it (issue #118).
            flight = preflight.check(options.verify, repo_dir, asked=text)
            the_plan.reasons.extend(f"check: {line}" for line in flight.plan_lines())
            if not flight.ok:
                the_plan.reasons.append(f"check: {flight.reason()}")
        return Report(None, "dry run", the_plan)
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(repo_dir)  # work run here: the project is locks'

    key = repo.key(repo_dir)
    c = the_plan.classification
    fields = dict(
        repo=key, cwd=str(repo_dir), kind=the_plan.kind, complexity=c.complexity, class_source=c.source,
        class_reason=c.reason, start_cell=the_plan.start.label, pinned=int(options.start is not None),
        points=the_plan.points.points, points_why=", ".join(the_plan.points.reasons),
    )
    queued_s = None
    # The time before this run that no phase of its own measures: from the step it waited on
    # passing to its being queued, from queued to the dispatcher starting it, and from that to here.
    gaps = _gaps(store, task_id, began) if task_id is not None else {}
    if task_id is not None:
        # A queued or resumed task: it keeps its id, its messages, its cost so
        # far and its place in the lane's history.
        before = store.get_task(task_id)
        if before["status"] == "queued" and not store.last_event(task_id, "finished"):
            queued_s = _seconds_since(before["created_at"])
        # Running and whose process it is, in one write: between two, a sweep read a running task with
        # no pid as dead and stopped a resume that had just started (it then died unseen).
        store.update_task(task_id, status="running", source="locks", pid=os.getpid(),
                          cost_usd=float(before["cost_usd"] or 0) + c.cost_usd, **fields)
        task = store.get_task(task_id)
    else:
        # Run from a session's own Bash (`LOCKS_SESSION_ID` set), the text is the main
        # session's, not something a person typed.
        task = store.create_task(text, status="running", source="locks", cost_usd=c.cost_usd, pid=os.getpid(),
                                 author="orchestrator" if session_id else "person",
                                 session_id=session_id, options=json.dumps(options.stored()), **fields)
    report = Report(task["id"], "running", the_plan, cost_usd=c.cost_usd)
    say = progress or (lambda line: None)
    # Said as it happens: a run in a background shell is otherwise silent until
    # its report, and silence reads as a run that never started.
    say(f"locks: task #{task['id']} started: {the_plan.kind}, at {the_plan.start.label}")
    if task_id is not None and not store.last_event(task["id"], "planned") and (
            released := after.released_line(store, task["id"])):
        # Queued after other work that passed: where it starts, said once, as it starts.
        the_plan.reasons.append(released)
    if options.resume_why:
        the_plan.reasons.append(options.resume_why)
    if task_id is None:
        for twin in _twins(store, task, repo_dir):
            the_plan.reasons.append(f"task #{twin} asked the same and keeps its work on {isolate.branch_for(twin)}: "
                                    f"`locks resume {twin}` continues it instead of starting over")
    if options.max_turns and options.max_turns > MAX_TURNS_CEILING:
        the_plan.reasons.append(f"{options.max_turns} turns were asked for; an attempt gets at most "
                                f"{MAX_TURNS_CEILING}: work bigger than that is split")
    if not options.max_turns and the_plan.turns is None and turns_for(the_plan.kind) != DEFAULT_TURNS:
        the_plan.reasons.append(f"{the_plan.kind} work starts with {turns_for(the_plan.kind)} turns: "
                                f"at {DEFAULT_TURNS} its attempts ran out")
    if told := manifests.plan_line(manifests.read_all()):
        the_plan.reasons.append(told)
    the_plan.quiet = said_today(store, repo.key(repo_dir), [*the_plan.reasons,
                                                           *(st.line() for st in the_plan.neighbours)])
    warned = warnings(the_plan.reasons)
    for reason in warned:
        if reason not in the_plan.quiet:
            say(f"locks: #{task['id']} warning: {reason}")
    store.add_event(task["id"], "planned", kind=the_plan.kind, start=the_plan.start.label, warnings=warned,
                    role=the_plan.role.name if the_plan.role else None, team=the_plan.team,
                    risk=the_plan.risk.data(), points=the_plan.points.points,
                    points_why=list(the_plan.points.reasons), standard=the_plan.standard,
                    ladder=[x.label for x in the_plan.ladder], reasons=the_plan.reasons,
                    neighbours=[st.line() for st in the_plan.neighbours], plan_s=round(plan_s, 2),
                    quiet=sorted(the_plan.quiet),
                    **({"probation": the_plan.probation} if the_plan.probation else {}),
                    **({predict_mod.KEY: the_plan.predicted} if the_plan.predicted else {}),
                    queued_s=queued_s)

    workspace = None
    filled: list[str] = []
    resumed = False
    # Why the task's worktree could not be made, in one line. It stops the task before its first
    # attempt, resumable, instead of leaving it `running` under a process that is about to exit.
    no_worktree: str | None = None
    writes = the_plan.kind not in READ_ONLY_KINDS
    # A build in a repository the person made spec-first owes its spec (`locks.specs`).
    spec_first = (writes and the_plan.kind in specs_mod.FIRST_KINDS and config.enabled("specs", os.environ)
                  and (repo_dir / specs_mod.OPENSPEC).is_dir() and specs_mod.first(store, key))
    declared_read_only = declares_read_only(the_plan.kind, task.get("body") or task.get("title") or "")
    if not options.verify and options.run_verify and writes and not declared_read_only:
        # A workflow step with no kind of its own was classified into one that writes:
        # the run's check is its check.
        options = dataclasses.replace(options, verify=options.run_verify)
    context = list(the_plan.context)
    # A reading step of a workflow reads the work of the steps before it: it gets
    # their commit too, in a worktree its tools cannot write to.
    if options.isolate and (writes or options.base) and repo.toplevel(repo_dir) is not None:
        try:
            # A task that never ran owns no branch yet: one of its name is another's.
            never_ran = (len(store.events_of(task["id"], "planned")) <= 1 and not store.attempts(task["id"])
                         and not store.events_of(task["id"], "finished"))
            resumed = not never_ran and isolate.kept(repo_dir, task["id"])
            workspace = isolate.prepare(repo_dir, task["id"], home(), base=options.base, fresh=never_ran)
            if workspace.set_aside:
                say(f"a branch {workspace.branch} was already in this checkout with none of this task's work "
                    f"(another locks store's): kept as {workspace.set_aside}, and this task starts on its base")
                store.add_event(task["id"], "branch_set_aside", branch=workspace.branch, to=workspace.set_aside)
            if workspace.stray:
                say(f"this task's folder {workspace.path} was left by a run whose removal stopped half way, and "
                    f"git no longer knew it: moved to {workspace.stray} (delete it once nothing in it is wanted)")
                store.add_event(task["id"], "stray_moved", path=str(workspace.path), to=str(workspace.stray))
            if isolate.has_compose(workspace.path):
                context.append("This worktree has no container stack up: the one the person runs belongs to their "
                               "checkout. Do not start another (sail up, docker compose up) to run the check; if "
                               "the check needs one, stop and report the environment. locks stops any stack "
                               "named after this worktree when the task ends.")
            if resumed:
                context.append("This task was stopped before and is resumed: what its earlier attempts wrote is "
                               "already here. Check it, and finish it rather than starting over.")
            if workspace.links:
                context.append("Linked from the person's checkout and shared with it: "
                               + ", ".join(workspace.links) + ". Use them; do not delete or reinstall them.")
            if workspace.stale:
                # Linking them would hand the worker another commit's dependencies.
                context.append("Not linked from the person's checkout: " + ", ".join(workspace.stale)
                               + ". The lockfile here differs from the one they were installed from there, so "
                               "install the dependencies in this directory first (`npm ci`, `uv sync`, as the "
                               "project does).")
                say(f"dependencies not linked, their lockfile differs from your checkout's: "
                    f"{', '.join(workspace.stale)}; the worker installs them (a refused install is allowed "
                    f"with --allow, e.g. 'Bash(npm ci)')")
            if not resumed:
                filled = _fill_worktree(store, task["id"], workspace, text, options.carry, say)
                context += filled
        except (isolate.IsolationError, OSError) as exc:  # git failed, or git or the disk is not there
            no_worktree = " ".join(str(exc).split())
    if workspace is not None and task.get("workflow_run"):
        # How far the base branch moved past the work this step starts from: said, never merged.
        drift = isolate.behind_text(workflows.drift(store, task, workspace.base, "start") or {})
        if drift:
            say(f"locks: #{task['id']} starts {drift}")
    workdir = workspace.path if workspace else repo_dir
    context += how_to_run(workdir, (*options.allow_tools, *grants.granted(key)))
    # The thread of the session that sent it, as the scribe kept it: the worker
    # does not start from the task's text alone.
    from_session, got = _session_context(task, text)
    context += from_session
    launch_dir = options.launch_dir.resolve() if options.launch_dir else workdir

    # Strict, as the prompt hook: a task's text shares a word or two with
    # nearly every task in its repository, and a loose match puts unrelated
    # failures in front of the worker as if they were this problem's. Matched by
    # the work's own text (a step's goal, never its template) and kept to its
    # cell: the failures of its area, of its role, and the shared ones.
    reach = cells.of_request(text, the_plan.kind, touched_paths(text, options))
    dead = cells.dead_ends(store, cells.memory_text(store, task), repo=key, areas=reach,
                           role=the_plan.role.name if the_plan.role is not None else None)
    if dead:
        # What the workers are shown, kept as shown: the memory grows after the run.
        store.add_event(task["id"], "dead_ends", shown=dead)
    # How the cell's earlier workers got oriented: given or withheld by the primer's
    # trial, and kept either way so the two arms can be compared.
    role_name = the_plan.role.name if the_plan.role is not None else None
    primed: dict[str, Any] = {}
    if config.enabled("primer", os.environ):
        lines = cells.primer(store, key, role=role_name, areas=reach)
        if lines:
            arm, why = cells.primer_arm(store, key, task["id"])
            primed = {"primer_arm": arm, "primer_lines": len(lines), "primer_why": why}
            if arm == "primer":
                context.append(cells.primer_text(lines))
    # Which role and which version of its text this worker wears, and the role's lessons here:
    # given or withheld by their trial, and kept either way so the arms can be compared.
    # The notes the brief carries are kept too, so a worker-learned one is proven or dropped by
    # the tasks that read it (`notes.weigh`), never by a guess.
    context.extend(resume_context(store, task, key))
    worn: dict[str, Any] = {}
    held_files = roles.languages(repo_dir) if the_plan.role is not None else None
    if the_plan.role is not None:
        worn = {"role": role_name, "role_version": roles.version(the_plan.role),
                "skill_sources": roles.skill_sources(the_plan.role, held=held_files)}
        if revisions.enabled():
            lessons, given = revisions.for_brief(store, key, role_name, task["id"])
            worn.update(given)
            if lessons:
                context.append(lessons)
    # A workflow step whose first try forks the session of the reading step it names
    # (`locks.trunks`): forked or fresh by the trunk's trial, kept either way so the arms
    # can be compared; one with nothing to fork says why and is in no arm.
    forking: dict[str, Any] | None = None
    trunked: dict[str, Any] = {}
    if options.trunk and trunks.enabled() and not store.attempts(task["id"]) and not resumed:
        trunk = options.trunk
        why = trunk.get("missing") or ("" if workspace is not None else
                                       "it runs in no worktree of its own, where a session is kept")
        held_tokens = trunks.context_tokens(trunk["session"]) if not why else None
        if held_tokens is not None and held_tokens > trunks.MAX_TOKENS:
            why = f"its trunk's context held {held_tokens} tokens, over {trunks.MAX_TOKENS}: little room left"
        if why:
            trunked = {"trunk_why": f"starts fresh, not as a fork of step {trunk.get('step')!r}: {why}"}
        else:
            arm, arm_why = trunks.arm(store, key, task["id"])
            trunked = {trunks.KEY: arm, "trunk_why": arm_why, "trunk_task": trunk.get("task"),
                       **({"trunk_tokens": held_tokens} if held_tokens is not None else {})}
            if arm == "fork":
                forking = dict(trunk)
        say(f"locks: #{task['id']} " + (f"forks step {trunk.get('step')!r}'s session (#{trunk.get('task')}) "
                                         f"on its first try: {trunked['trunk_why']}" if forking is not None
                                         else trunked["trunk_why"]))
    # A worker handed a neighbour whose measurement its work owes (`locks.evidence`): held to it or
    # free by the trial, kept either way so the arms can be compared.
    evidenced: dict[str, Any] = {}
    held_to: list[evidence_mod.Requirement] = []
    if writes and evidence_mod.enabled() and (owing := evidence_mod.handed(the_plan.capabilities)):
        arm, arm_why = evidence_mod.arm(store, key, task["id"])
        evidenced = {evidence_mod.KEY: arm, "evidence_why": arm_why,
                     "evidence_of": [r.capability for r in owing]}
        if arm == "gate":
            held_to = owing
            context.append(evidence_mod.brief_line(owing))
    # A method its kind's workers are briefed to follow (`locks.methods`): given or not by its trial,
    # kept either way so the arms can be compared.
    methodical: dict[str, Any] = {}
    if methods_mod.enabled() and (method := methods_mod.for_kind(the_plan.kind)) is not None:
        side, side_why = methods_mod.arm(store, key, method, task["id"])
        methodical = {methods_mod.KEY: f"{method.name}:{side}", "method_why": side_why}
        if side == "with":
            context.append(method.lines)
    the_plan.seen_risks = tuple(methods_mod.lines_for(methods_mod.run_risks(store, task)))
    # Whether this worker is handed livespec's tools (`locks.handing`): drawn by the trial of its
    # nature here, kept either way so the arms can be compared; the orientation goes with the tools.
    # What locks read from the index itself (the code map, the size, the check's impact) stays.
    handed_arms: dict[str, Any] = {}
    withheld_tools: frozenset[str] = frozenset()
    if any(a.name == "livespec" for a, _ in the_plan.active) and handing_mod.enabled():
        side, side_why = handing_mod.arm(store, key, the_plan.kind, task["id"])
        handed_arms = {handing_mod.KEY: f"{handing_mod.nature(the_plan.kind)}:{side}", "livespec_why": side_why}
        if side == "withhold":
            withheld_tools = frozenset({"livespec"})
    context[:0] = [line for name, line in the_plan.oriented if name not in withheld_tools]
    # The brief's fixed weight, per kind and release (`locks.footprint`): what a worker reads
    # before its task, so a brief that grows release after release is seen, not guessed.
    worn_brief = roles.brief(the_plan.role, held=held_files) if the_plan.role is not None else None
    store.add_event(task["id"], "briefed", session=got, dead_ends=len(dead), areas=list(reach),
                    context_chars=sum(len(line) for line in context), kind=the_plan.kind,
                    system_chars=len(worn_brief.system_prompt) if worn_brief is not None else 0,
                    skills=len(worn_brief.skills) if worn_brief is not None else 0,
                    # Each adopted neighbour's state when this brief was written: a brief with no
                    # livespec orientation says why (absent, stale, unreadable), never left to guess.
                    **({"livespec_tools": sorted(handed)} if (handed := next(
                        (a.worker_tools(the_plan.kind, st) for a, st in the_plan.active
                         if hasattr(a, "worker_tools") and a.name not in withheld_tools), None)) is not None
                       else {}),
                    # livespec's index describes the person's checkout; a worker in a worktree edits
                    # a copy of it, so what the index answers is the base, not its own work so far.
                    **({"livespec_index_of": "checkout" if workdir.resolve() != repo_dir.resolve() else "workdir"}
                       if handed is not None else {}),
                    **({"neighbours": {st.name: st.state + (f": {clip(st.detail, 160)}" if st.detail else "")
                                       for st in the_plan.neighbours}} if the_plan.neighbours else {}),
                    **primed, **worn, **trunked, **evidenced, **handed_arms,
                    **methodical, **(the_plan.recalled or {}),
                    **({notes.BRIEFED: [int(n["id"]) for n in the_plan.notes]} if the_plan.notes else {}),
                    **({notes.WITHHELD: list(the_plan.withheld)} if the_plan.withheld else {}))
    # The runtime the project declares goes first on the worker's PATH, and on its
    # check's: a worker given the wrong Node hunts for another and is refused each one.
    run_env = runtime.env_for(workdir)
    briefing = worn_brief
    # The independent check: what it found first, and how many corrections it has had.
    verifying = writes and not declared_read_only and config.enabled("verifier", os.environ) \
        and not options.reviewed_next
    if writes and options.reviewed_next and config.enabled("verifier", os.environ):
        the_plan.reasons.append("no independent check of its own: the blind review step that follows reads "
                                "this change as the verifier would")
    # The independent check after the ending (`late_verifier`): the run ends on locks' own
    # check and the verifier reads the kept branch afterwards. Only with a check of locks' to end
    # on, and never for a workflow step, a fix that answers ledger rows, or work something waits
    # on: a step or held work it released could not be taken back.
    late = verifying and bool(options.verify) and config.enabled("late_verifier", os.environ) \
        and not task.get("workflow_run") \
        and not _fixing(options) and not options.ledger and not _awaited(store, task["id"])
    deferred: dict[str, Any] | None = None
    ledger: dict[str, Mapping[str, str]] = {findings_mod.key(f): f for f in known_findings}
    corrections = 0
    # A workflow step that decides ledger rows: a pass answers each, and its answers decide the run.
    deciding = options.ledger if options.ledger and options.ledger.get("role") in ledger_mod.WORDS else None
    settled: ledger_mod.Outcome | None = None
    base_commit = workspace.base if workspace else _head(repo_dir)
    # In a folder that is no checkout, the base is each repository inside it at its own commit.
    base_places = _base_places(repo_dir) if workspace is None and base_commit is None else ()
    # A red check is compared with the same check at the commit the work started from:
    # what failed there too was not this work's doing (`_against_base`).
    baseline = Baseline(store, task["id"], workspace.repo_dir if workspace else repo_dir,
                        base_commit or isolate.places_label(base_places), home(),
                        must_fix=the_plan.kind in _REPRODUCES,
                        runs=flake_fix_runs(the_plan.kind, task.get("body") or task.get("title") or ""),
                        places=base_places) \
        if (base_commit or base_places) and options.verify else None
    # Turns are the one room dial, and they have one ceiling: a number asked for from
    # outside (a resume, a model's tool call) never raises it.
    cell, turns = the_plan.start, min(options.max_turns or the_plan.turns or turns_for(the_plan.kind),
                                      MAX_TURNS_CEILING)
    attempts: list[Attempt] = []
    changed: set[str] = set()
    started_at: list[dict[str, Any]] | None = None
    decision: Decision | None = None
    # What the brief says of work set aside when a new model started from the base.
    aside: list[str] = []
    # The last attempt's session, and what ran it: more turns on the same cell continue
    # it, more effort on the same model continues a fork of it.
    session: WorkerSession | None = None
    # The session the next attempt continues, or why one due starts fresh: decided with the move.
    resume, fresh = "", ""
    # The commit's subject: the last line a worker proposed for the change.
    subject = ""
    # In the checkout itself, one run at a time: the work and its check share the
    # person's files. A run in a worktree has its own.
    in_place = no_worktree is None and workspace is None and (
        (writes and not declared_read_only) or bool(options.verify))
    held = contextlib.ExitStack()
    # What an integrate step joined before its first attempt (`_integrate`).
    joined: dict[str, Any] | None = None
    timing = {"hold_s": 0.0, "services_s": 0.0, "worker_s": 0.0, "check_s": 0.0, "verifier_s": 0.0, "wait_s": 0.0}
    # Its first try races the next rung (`race_cell`): only a fresh run in its own worktree
    # that brought nothing in from the person's checkout, which the race's lane would lack.
    racing = the_plan.race is not None and workspace is not None and not history and not filled and not resumed \
        and forking is None
    try:
        mark = time.monotonic()
        if in_place:
            for place in isolate.held_by(repo_dir, repo.toplevel(repo_dir)):
                held.enter_context(isolate.in_place(
                    home(), place, task["id"], waiting=lambda who: _waits(store, task["id"], who, say),
                    check=lambda: _check_cancel(store, task["id"])))
        timing["hold_s"] = time.monotonic() - mark
        ready = no_worktree is None
        if not ready:
            report.stop = stops.Stop("environment", f"its worktree could not be made: {no_worktree}"[:500],
                                     extra={"starved": "disk"} if launch.NO_SPACE.search(no_worktree or "") else {})
            report.status, report.summary = "blocked", report.stop.reason
        elif options.verify and not (flight := _preflight(
                options.verify, workdir, task, store, say,
                installing=bool(workspace is not None and workspace.stale))).ok:
            # The check could not start at all: no worker is spent to find that out.
            report.stop = stops.Stop("preflight", flight.reason(), extra={"fixes": [b.fix for b in flight.blockers]})
            report.status, report.summary, ready = "blocked", report.stop.reason, False
        elif options.services:
            mark = time.monotonic()
            try:
                up = held.enter_context(services_mod.running(
                    [services_mod.parse(s) for s in options.services], workdir,
                    home() / "work" / f"services-{task['id']}.log", run_env))
            except services_mod.ServiceError as exc:
                # The servers the task needs never came up: nothing a worker does helps.
                report.stop = stops.Stop("environment", str(exc))
                report.status, report.summary, ready = "blocked", str(exc), False
            else:
                timing["services_s"] = time.monotonic() - mark
                say(f"locks: #{task['id']} servers up: " + "; ".join(up))
                context.append("Servers up for this task, started and stopped by locks (do not start or stop them "
                               "yourself): " + "; ".join(up))
        if ready and options.integrate:
            # The parts' branches are joined before any worker: one is needed only for a conflict,
            # or for a check that fails on the joined work.
            joined, ready = _integrate(store, task["id"], workspace, options, report, context, say, baseline)
        while ready:
            # A try after the network dropped, or the load timed its tests out, is no
            # attempt at the work: it does not count toward the ceiling, and its own
            # streak is bounded in `decide`.
            if sum(not waited(a) for a in attempts) >= options.max_attempts:
                if attempts and attempts[-1].blockers:
                    # The work passed its own check; only the independent check's blockers stand.
                    report.stop = stops.Stop(
                        "review", review_reason(attempts[-1], f"the limit of {options.max_attempts} attempts"),
                        attempt=len(attempts), account=findings_mod.text(attempts[-1].blockers)[:2000])
                    report.status, report.summary = "blocked", report.stop.reason
                else:
                    report.stop = stops.Stop("attempts", f"{options.max_attempts} attempts without a pass",
                                             attempt=len(attempts))
                    report.status, report.summary = "failed", report.stop.reason
                break
            _check_cancel(store, task["id"])
            selection = _equip(_with_role(caps.select(
                registry, the_plan.kind, failed_before=bool(attempts), workdir=workdir,
                paths=touched_paths(text, options, [*changed, *(isolate.changed(workspace) if workspace else ())]),
                reading=not writes or declared_read_only), the_plan.role, registry, workdir), the_plan, repo_dir,
                workdir, withheld=withheld_tools)
            # A tool that acts answers to the loop the task is a round of, or to none.
            selection = caps.for_loop(selection, options.loop, home())
            # Numbered as the store numbers it: a resumed task's attempts go on from its last, and
            # from one whose worker died too (`Store.next_attempt`), so its calls stay its own.
            nth = store.next_attempt(task["id"])
            fork = bool(resume) and decision is not None and decision.move is Move.MORE_EFFORT
            # A later attempt also reads the notes anchored to the files the work changed so far, which
            # the first brief could not name (`notes.anchored_to`).
            later = _later_notes(store, key, the_plan, [*changed, *(isolate.changed(workspace) if workspace
                                                                     else ())]) if attempts else []
            # The notes this attempt's brief carried, the only ones its `notes_used` may name.
            briefed_notes = {int(n["id"]) for n in the_plan.notes} | {
                int(m) for line in later for m in re.findall(r"^- #(\d+) ", line)}
            # The blockers a `review` stop kept reach this run's first worker; a later one is told
            # what its own attempts met (`failed_output`, a correction's blockers).
            reviewed = review_context(known_findings) if not attempts else []
            prompt = brief(text, the_plan.kind, workdir, [*history, *attempts], dead, options.verify,
                           writes and not declared_read_only, [*context, *reviewed, *aside, *later],
                           options.surfaces)
            role_text = briefing.system_prompt if briefing else ""
            hints = selection.system_prompt()
            if options.trunk_for and (role_text or hints):
                # Later steps fork this session, and a fork keeps the system prompt its first
                # request recorded: its role and hints go in its brief, so a fork reads one role.
                prompt = "\n\n".join(p for p in (role_text and "Your role for this step:\n" + role_text, hints,
                                                  prompt) if p)
            # Only the first try forks the trunk; a later one goes on from its own attempts.
            branch = forking if forking is not None and not attempts else None
            spec = launch.LaunchSpec(
                model_id=models.pinned(cell.model),
                permission_mode=grants.mode_for(cell.model),
                prompt=prompt,
                cell=cell,
                launch_dir=launch_dir,
                workdir=workdir if workdir != launch_dir else None,
                max_turns=turns,
                timeout_s=launch.timeout_for(turns),
                # A reading worker's answer is its deliverable: kept whole in a `locks-report` block
                # (an internal role, the surveyor, has a report shape of its own).
                report=the_plan.kind in READ_ONLY_KINDS
                and (the_plan.role is None or the_plan.role.name not in roles.INTERNAL),
                disallowed_tools=tuple(dict.fromkeys([*(() if writes else launch.WRITE_TOOLS),
                                                      *(the_plan.role.disallowed_tools if the_plan.role else ())])),
                # The person's rules, for this run and for this repository, the
                # tools of every MCP server locks itself hands this worker (a
                # capability it was given and then refused is no capability), and
                # reading the skill folders of its role.
                allowed_tools=tuple(dict.fromkeys([*options.allow_tools, *grants.granted(key),
                                                   *(f"mcp__{name}" for name in selection.servers),
                                                   *(roles.read_rules(briefing) if briefing else ()),
                                                   *(f"Read(/{Path(d).resolve()}/**)" for d in selection.read_dirs)])),
                extra_dirs=(*(tuple(workspace.repo_dir / rel for rel in workspace.links
                                    if (workspace.repo_dir / rel).is_dir()) if workspace else ()),
                            *(Path(d).expanduser().resolve() for d in options.add_dirs)),
                mcp_servers=selection.servers,
                append_system_prompt="" if options.trunk_for else "\n\n".join(p for p in (role_text, hints) if p),
                env={**run_env, "LOCKS_WORKER_TASK": str(task["id"]), "LOCKS_WORKER_ATTEMPT": str(nth)},
                criteria=criteria_mod.ids(text),
                judging=not writes or declared_read_only or deciding is not None,
                verdicts=tuple(deciding.get("ids") or ()) if deciding else (),
                verdict_words=ledger_mod.WORDS.get(deciding["role"], ()) if deciding else (),
                parts=options.parts,
                # Kept only in a worktree, where its directory is the task's: a session in the
                # person's checkout would be one of theirs.
                persist_session=workspace is not None,
                resume_session=branch["session"] if branch else resume,
                fork_session=bool(branch) or fork,
                # A fork of the trunk is told it is a new worker, and its role: its system prompt is
                # the trunk's record. Its brief follows.
                resume_prompt=(trunks.note(branch, role=role_text, capabilities=hints, workdir=workdir,
                                           base=workspace.base if workspace else None) if branch
                               else launch.DEEPER_PROMPT if fork else _reshape_prompt([*history, *attempts])
                               if decision is not None and decision.reshape else launch.CONTINUE_PROMPT),
                resume_note=prompt if branch else continuation_note([*history, *attempts][-1]) if resume else "",
            )
            store.update_task(task["id"], current_cell=cell.label)
            say(f"locks: #{task['id']} attempt {nth} at {cell.label}")
            # With no worktree, where each checkout the run may change stood when it started: what the
            # worker wrote is read in them (`isolate.moved_in_place`) as a worktree is read against its
            # base. Taken once, at the first attempt: a later one starts on its earlier attempts' work,
            # which is the task's own and no more the person's.
            if in_place and started_at is None:
                started_at = isolate.stand(repo_dir, in_place_checkouts(repo_dir))
            places = started_at or []
            store.add_event(task["id"], "attempt_started", seq=nth, cell=cell.label,
                            **({"base": workspace.base, "links": list(workspace.links)} if workspace else {}),
                            **({"places": places} if places else {}),
                            model=spec.model_id or cell.model,
                            max_turns=turns, capabilities=list(selection.names),
                            since_start_s=round(time.monotonic() - clock, 2), brief_chars=len(spec.prompt),
                            **({"role": the_plan.role.name, "team": the_plan.team,
                                "skills": list(briefing.skills)} if briefing else {}),
                            **({"resumed": resume} if resume else {}), **({"fresh": fresh} if fresh else {}),
                            **({"forked_trunk": {k: branch.get(k) for k in ("task", "step", "session")}}
                               if branch else {}))
            if resume:
                say(f"locks: #{task['id']} attempt {nth} continues attempt {nth - 1}'s session ({resume})"
                    + (f" at {cell.label}" if fork else ""))
            elif fresh:
                say(f"locks: #{task['id']} attempt {nth} starts fresh: {fresh}")
            mark = time.monotonic()
            raced, raced_first = None, False
            if racing and the_plan.race is not None and workspace is not None:
                racing = False
                store.add_event(task["id"], "attempt_started", seq=nth + 1, cell=the_plan.race.label, race_of=nth,
                                model=models.pinned(the_plan.race.model) or the_plan.race.model, max_turns=turns,
                                since_start_s=round(time.monotonic() - clock, 2))
                say(f"locks: #{task['id']} attempt {nth} races {the_plan.race.label} beside {cell.label}")
                raced = _race(launcher, spec, cell, the_plan.race, workspace, options.verify, baseline)
            if raced is not None:
                # What follows (the independent check, the next attempt) works in the task's worktree,
                # which holds the kept try's work: the spec keeps its place, with the kept try's cell.
                result = raced.result
                spec = dataclasses.replace(spec, cell=raced.cell, model_id=raced.spec.model_id,
                                           permission_mode=raced.spec.permission_mode)
                if raced.cell != cell:
                    # The higher rung's try goes on: the start's is recorded before it.
                    _record_raced(store, task["id"], raced, turns, selection.names, report, first=True)
                    attempts.append(_raced_attempt(raced, turns))
                    nth, raced_first = nth + 1, True
                cell = raced.cell
            else:
                result = launcher(spec)
            if result.fresh:
                say(f"locks: #{task['id']} attempt {nth} started fresh: {result.fresh}")
            session = WorkerSession(result.session_id if spec.persist_session else "", spec.model_id or cell.model,
                                    cell, nth)
            worker_s = time.monotonic() - mark
            # Out of turns (or time) before its own check: whatever the check finds next,
            # the work had no room to finish, and room is what the next attempt gets.
            outgrew = result.failure is Failure.TURNS_EXHAUSTED
            mark = time.monotonic()
            # What locks' own check writes in the worktree is the check's output, not the
            # worker's: it is committed with the work, and said, since a check that rewrites
            # tracked files may not give the same files on the person's checkout.
            before = isolate.fingerprint(workspace.path) if workspace is not None and options.verify else None
            if baseline is not None and not (raced is not None and raced.checked):
                baseline.found = None
            ahead = None
            # Whether locks' own check already ran, and was compared with the base, on this attempt.
            checked_here = bool(raced is not None and raced.checked)
            if (result.passed and options.verify and not (raced is not None and raced.checked)) or (
                    _settles(result, options.verify) and result.changed_paths):
                # Where the attempt is now, for whoever watches it (`locks.progress`): its worker is done.
                store.add_event(task["id"], "stage", seq=nth, stage="check")
            if result.passed and options.verify and not (raced is not None and raced.checked):
                ahead = None if late else _ahead(
                    result, options=options, verifying=verifying, workspace=workspace, cell=cell, plan=the_plan,
                    text=text, spec=spec, workdir=workdir, base=base_commit, ledger=ledger, launcher=launcher,
                    registry=registry,
                    store=store, task=task, corrections=corrections,
                    since=_read_before(store, task["id"], cell, corrections, workdir if workspace else None))
                result = _verified(result, options.verify, workdir, baseline)
                checked_here = True
            elif _settles(result, options.verify) and (
                    isolate.has_changes(workspace) if workspace else bool(result.changed_paths)):
                checked_here = True
                unchecked = "it was refused a command" if result.failure is Failure.PERMISSION else \
                    "it ran out of turns"
                result = _settled(result, options.verify, workdir, baseline)
                report.unchecked = unchecked if result.passed else ""
            if not (result.passed or checked_here):
                result = _weigh_base_claim(result, options.verify, workdir, baseline, workspace, spec.criteria)
            stands = ""
            if result.failure in REPLAN and not result.denied and (
                    isolate.has_changes(workspace) if workspace else bool(result.changed_paths)):
                # A worker that did the work and names a point it cannot settle: does the work stand?
                stands = _stands(result, options.verify, workdir, baseline)
                if stands == "locks" and _only_the_base(result, options.verify, baseline):
                    # Its open point is the check's failures, and they were all there before
                    # the work: nothing is left to decide, and the work passes as it stands.
                    result = dataclasses.replace(
                        result, passed=True, failure=None, verdict="pass",
                        summary=f"{result.summary} (locks: the check's failures it names all fail at the base "
                                "too, so they were there before this work and no decision is left)")
                    stands = ""
            # What the report and the verdict say of a check that failed only as it does at the base.
            report.preexisting = baseline.found if baseline is not None and (result.passed or stands) else None
            if before is not None and workspace is not None and \
                    (wrote := isolate.rewritten(before, isolate.fingerprint(workspace.path))):
                report.check_output = sorted({*report.check_output, *wrote})
                store.add_event(task["id"], "check_rewrote", command=options.verify, n=len(wrote),
                                files=list(wrote[:200]))
            check_s = time.monotonic() - mark
            strayed = surfaces_mod.outside(options.surfaces, result.changed_paths)
            if strayed:
                if workspace is not None:
                    isolate.undo(workspace, strayed)
                kept = "undone" if workspace is not None else "left in your checkout, unverified"
                # A stray path is its own finding: it never hides how the check failed. An
                # environment failure, or a check that failed with its own output, keeps its
                # class and its output; only a pass or a failure with nothing to show reads as
                # the task misunderstood (issue #119).
                keeps = result.failure is Failure.ENVIRONMENT or (
                    result.failure is Failure.CODE_BUG and bool(result.check_tail))
                result = dataclasses.replace(
                    result, passed=False, failure=result.failure if keeps else Failure.APPROACH,
                    changed_paths=tuple(p for p in result.changed_paths if p not in strayed)
                    if workspace is not None else result.changed_paths,
                    summary=f"changed outside its surfaces ({', '.join(strayed[:5])}), {kept}: {result.summary}",
                    evidence=f"Surfaces: {', '.join(options.surfaces)}. Changed outside them: {', '.join(strayed)}"
                             + (f"\n{result.evidence}" if keeps and result.evidence else ""),
                    check_tail=result.check_tail if keeps else "")
                store.add_event(task["id"], "outside_surfaces", seq=nth, paths=strayed[:50],
                                surfaces=list(options.surfaces), undone=workspace is not None)
            if result.passed and spec_first and (owed := specs_mod.owed(the_plan.kind, sorted(
                    {*changed, *(isolate.changed(workspace) if workspace else result.changed_paths)}))):
                # A spec-first repository takes no build without its spec: the first such pass goes
                # back to the same worker, in its own session, to write it; a second stands as work
                # with one point for the orchestrator, kept on its branch.
                again = any(a.shape.startswith(specs_mod.FIRST_SHAPE) for a in attempts)
                why = specs_mod.first_shape(owed)
                result = dataclasses.replace(
                    result, passed=False, verdict="",
                    failure=Failure.SPEC_BUG if again else Failure.INCONCLUSIVE,
                    shape="" if again else why,
                    summary=f"{why}{' again' if again else ''}: {result.summary}")
                if again:
                    stands = "locks" if options.verify else "worker"
                store.add_event(task["id"], "spec_owed", seq=nth, paths=owed[:50], again=again)
            if result.passed and held_to and (unmet := evidence_mod.owed(
                    store, task["id"], nth, selection.names,
                    sorted({*changed, *(isolate.changed(workspace) if workspace else result.changed_paths)}),
                    the_plan.kind)):
                # Held to evidence (`locks.evidence`): a pass with no call of the neighbour it owes goes
                # back to the same worker once, in its own session; a second stands as work with one
                # point for the orchestrator, kept on its branch. Never a climb.
                again = any(a.shape.startswith(evidence_mod.SHAPE) for a in attempts)
                why = evidence_mod.shape(unmet)
                result = dataclasses.replace(
                    result, passed=False, verdict="",
                    failure=Failure.SPEC_BUG if again else Failure.INCONCLUSIVE,
                    shape="" if again else why,
                    summary=f"{why}{' again' if again else ''}: {result.summary}")
                if again:
                    stands = "locks" if options.verify else "worker"
                store.add_event(task["id"], "evidence_owed", seq=nth, capabilities=[r.capability for r in unmet],
                                again=again)
            review_stop = None
            open_blockers: tuple[dict[str, str], ...] = ()
            mark = time.monotonic()
            worker_cost = result.cost_usd
            if result.passed and verifying:
                so_far = list(isolate.changed(workspace)) if workspace else sorted({*changed, *result.changed_paths})
                _risk_of_change(the_plan, so_far if (workspace or base_commit) else None, workdir, base_commit)
                needed = bool(so_far) and risk_mod.needs_verifier(the_plan.risk, cheap=_cheap(cell))
                sampled = _sampling(store, task, the_plan, cell, options, corrections, ledger) if needed else None
                if sampled is not None and not sampled.check:
                    # Its cell's record earned skip-lot sampling (`locks.trust`): locks' own check read this pass.
                    store.add_event(task["id"], "verifier_sampled", seq=nth, worker=cell.label, every=sampled.every,
                                    level=sampled.level.data(), risk=the_plan.risk.tier, why=sampled.why)
                    say(f"locks: #{task['id']} attempt {nth} passed; {sampled.why}")
                elif needed and late:
                    deferred = {"cell": cell, "spec": spec, "changed": so_far, "nth": nth,
                                "impact": _impact(the_plan, so_far, repo_dir, workdir=workdir, base=base_commit),
                                "index": _index_servers(the_plan, repo_dir, workdir),
                                "rules": [n for n in the_plan.notes if n.get("kind") == notes.CONSTRAINT]}
                    store.add_event(task["id"], "verifier_deferred", seq=nth, risk=the_plan.risk.tier,
                                    **({"sampled": sampled.why} if sampled is not None and sampled.why else {}))
                    say(f"locks: #{task['id']} attempt {nth} passed; risk {the_plan.risk.tier}: its independent "
                        "check runs after its ending")
                elif needed:
                    store.add_event(task["id"], "stage", seq=nth, stage="verifier")
                    # The check reads: what it writes in the work it reads is put back (`_read_only`).
                    read_from = isolate.snapshot(workspace.path) if workspace is not None else None
                    result, review_stop, ledger, corrections, open_blockers = _independent_check(
                        result, text=text, kind=the_plan.kind, cell=cell, spec=spec, workdir=workdir,
                        base=base_commit, changed=so_far, risk=the_plan.risk, ledger=ledger,
                        corrections=corrections, launcher=launcher, store=store, task_id=task["id"], nth=nth,
                        say=say, outgrew=outgrew, registry=registry,
                        rules=[n for n in the_plan.notes if n.get("kind") == notes.CONSTRAINT],
                        rows=_fixing(options), ahead=_tolerating(ahead, report.check_output),
                        impact=_impact(the_plan, so_far, repo_dir, workdir=workdir, base=base_commit),
                        index=_index_servers(the_plan, repo_dir, workdir), snapshots=workspace is not None,
                        sampled=sampled.why if sampled is not None else "")
                    ahead = None
                    if workspace is not None and read_from is not None:
                        _read_only(workspace.path, read_from, store, task["id"], nth, say)
            if ahead is not None:
                # locks' check failed (or the change needs no check now): what ran beside it is dropped.
                stopped = ahead.stop()
                spent = stopped.cost_usd if stopped is not None else 0.0
                result = dataclasses.replace(result, cost_usd=result.cost_usd + spent)
                store.add_event(task["id"], "verifier_dropped", seq=nth, cost_usd=round(spent, 4),
                                ran_s=round(ahead.beside_s, 2), why="locks' check did not pass")
            verifier_s = time.monotonic() - mark
            timing["worker_s"] += worker_s
            timing["check_s"] += check_s
            timing["verifier_s"] += verifier_s
            if result.passed and deciding:
                settled = ledger_mod.outcome(deciding, result.verdicts)
                store.add_event(task["id"], "ledger_decided", seq=nth, **settled.data())
                if settled.stop:
                    review_stop = stops.Stop("review", settled.stop, attempt=nth, account=clip(result.summary, 2000))
            changed.update(result.changed_paths)
            # What checked the work this attempt left: locks' check, or only the worker's own.
            report.checked = ("locks" if (result.passed and options.verify) or stands == "locks" else
                              "worker" if (result.passed and result.evidence.strip()) or stands == "worker" else "")
            report.cells.append(cell.label)
            report.served.append(result.served_model)
            report.cost_usd += result.cost_usd
            # The worker's tool calls reach the store only once the hooks' log is read into it: once
            # here, before anything of this attempt is read from it (its livespec calls, each
            # capability's, whether any call was logged, how it worked). A load that fails leaves the
            # record as it was; it never fails the run.
            with contextlib.suppress(Exception):
                habits.load_events(store)
            seq = store.add_attempt(
                task["id"], cell=cell.label, max_turns=turns, passed=int(result.passed),
                failure=result.failure.value if result.failure else None, summary=result.summary,
                evidence=kept_evidence(result.evidence), cost_usd=result.cost_usd,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens, turns=result.turns,
                duration_s=result.duration_s, changed_paths=result.changed_paths,
                served_model=result.served_model or None,
                capabilities=selection.names, session_id=session.id or None,
            )
            store.add_event(task["id"], "attempt_finished", seq=seq, cell=cell.label, passed=result.passed,
                            failure=result.failure.value if result.failure else None, cost_usd=result.cost_usd,
                            turns=result.turns, summary=result.summary[:500], denied=list(result.denied),
                            allow=list(result.allow), served_model=result.served_model,
                            permission_mode=result.permission_mode,
                            # Whether it went on in an earlier attempt's session; `fresh` says why one due did not.
                            continued=bool(spec.resume_session) and not result.fresh,
                            **({"fresh": result.fresh} if result.fresh else {}),
                            changed=list(result.changed_paths)[:50],
                            # Whether it used the code index it was handed, and which calls (`locks stats`).
                            **({"livespec_calls": used} if (used := _livespec_calls(
                                store, task["id"], nth, selection.names)) is not None else {}),
                            # Every capability it was handed, and the calls of its tools (`evidence.calls`).
                            **({"capability_calls": evidence_mod.calls(store, task["id"], nth, selection.names)}
                               if selection.names else {}),
                            # Whether its tool calls reached the log at all: a worker whose hooks did not
                            # run (no plugin where it ran) logs none, and its zeros are not zeros.
                            tools_logged=bool(store.tool_counts(task["id"], nth, "")),
                            # How its worker worked, from its own tool calls (`locks.behavior`): observed.
                            **({"behavior": shown} if (shown := behavior.of_attempt(store, task["id"], nth)) else {}),
                            # What locks' check said where it failed: shown by `locks show`, the board and
                            # the ending, and handed to the next attempt's brief.
                            **({"check_tail": result.check_tail} if result.check_tail else {}),
                            # The notes of its brief the worker says it relied on: observed, never a verdict;
                            # a number its brief never carried is left out.
                            # Kept as `[]` when it said none helped, and `notes_unsaid` when its block left the
                            # field out: a brief with notes whose worker said nothing is not "used none".
                            **({"notes_used": [n for n in result.notes_used if n in briefed_notes]}
                               if briefed_notes and result.notes_said else {}),
                            **({"notes_unsaid": True} if briefed_notes and not result.notes_said
                               and not result.shape else {}),
                            wall_s=round(worker_s, 2), duration_s=round(result.duration_s, 2),
                            check_s=round(check_s, 2), verifier_s=round(verifier_s, 2),
                            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                            cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens,
                            # The attempt's cost holds its independent check's; each is kept apart here.
                            worker_cost_usd=round(worker_cost, 4),
                            verifier_cost_usd=round(result.cost_usd - worker_cost, 4),
                            # What the dispatcher's level reads (`locks.capacity`): the API, or the machine, said no.
                            **({"limited": result.limited, "retry_after_s": result.retry_after_s}
                               if result.limited else {}),
                            **({"starved": result.starved} if result.starved else {}),
                            **({"refused_but_written": list(result.overcome)} if result.overcome else {}))
            attempt = Attempt(cell, turns, result.passed, result.failure, result.summary, result.denied,
                              allow=result.allow, changed=result.changed_paths, evidence=result.evidence[-1500:],
                              check_tail=result.check_tail,
                              transient=result.transient, findings=result.findings, network=result.network,
                              load=result.load, failing=result.failing, base_unknown=result.base_unknown,
                              shape=result.shape, limited=result.limited, retry_after_s=result.retry_after_s,
                              blockers=open_blockers, stands=bool(stands) and result.failure in REPLAN)
            attempts.append(attempt)
            if raced is not None and not raced_first:
                # The start's try went on: the rung's, stopped or failed, is recorded after it.
                store.set_move(task["id"], seq, "race", f"raced {raced.other_cell.label} beside it")
                _record_raced(store, task["id"], raced, turns, selection.names, report, first=False)
                if not result.passed:
                    # Kept in the history before the start's, which goes on: the move is read from
                    # the last attempt, and the rung's (only the environment stopped it) is not it.
                    attempts.insert(len(attempts) - 1, _raced_attempt(raced, turns))
            subject = result.commit or subject
            if result.report is not None:
                store.add_event(task["id"], "report", seq=seq, passed=result.passed, report=result.report)
            say(f"locks: #{task['id']} attempt {seq} " + ("passed" if result.passed else
                f"ended {(result.failure or Failure.INCONCLUSIVE).value}: {clip(result.summary, 160)}"))
            if review_stop is not None:
                report.stop = review_stop
                report.status, report.summary = "blocked", review_stop.reason
                break
            if result.passed:
                report.status, report.final_cell, report.summary = "done", cell.label, result.summary
                report.learned, report.parts, report.contract = result.learned, result.parts, result.contract
                report.criteria = result.criteria
                if result.constraints:
                    # A constraint is kept only when a check failed on this task before it passed: a
                    # rule nothing showed is a fact, kept as one.
                    if any(not a.passed or a.blockers for a in [*history, *attempts[:-1]]):
                        report.constraints = result.constraints
                    else:
                        report.learned = (*report.learned, *result.constraints)
                for adapter, status in the_plan.active:
                    report.impact += adapter.assess(result.changed_paths, repo_dir, status)
                # What git says the work changed, as the independent check is given it: a result's own
                # list is the worker's launch's and may hold less.
                wrote = list(isolate.changed(workspace)) if workspace else sorted({*changed, *result.changed_paths})
                report.impact += _links(the_plan, wrote, workdir, base_commit)
                if any(not a.passed and not a.raced for a in attempts) or store.task_has_failed_fix(task["id"]):
                    # A pass after failures, in this run or an earlier one (a resume that started
                    # fresh passes on its first attempt here): the problem they opened is solved. A race's
                    # other try is no failure of the work: a pass beside it opens no problem (`doctor`:
                    # `problem-opened-by-a-race`).
                    _remember(store, task, key, attempt, "worked", result, kind=the_plan.kind,
                              role=role_name)
                _solve_matched(store, task, key, cell, result)
                if result.orientation:
                    cells.record_orientation(store, task["id"], result.orientation, role=role_name,
                                             areas=cells.of_paths(result.changed_paths) or reach)
                break
            if result.failure in _MEMORABLE and result.summary:
                _remember(store, task, key, attempt, "failed", result, kind=the_plan.kind,
                          role=role_name)
            reading = not writes or (declared_read_only and not any(a.changed for a in attempts))
            decision = decide(the_plan.ladder, attempts, allow_approval=options.allow_approval, reading=reading,
                              several=_several(the_plan, options, task), skips=_skips(store, key, the_plan.kind))
            resume, fresh = _continues(decision, session)
            store.set_move(task["id"], seq, decision.move.value, decision.reason)
            to = decision.cell.label if decision.cell and decision.continues else None
            report.moves.append(f"{decision.move.value}" + (f" to {to}" if to else "") + f": {decision.reason}")
            report.decisions.append(decision)
            store.add_event(task["id"], "moved", seq=seq, move=decision.move.value, reason=decision.reason,
                            next_cell=decision.cell.label if decision.cell else None,
                            from_cell=cell.label, to_cell=to, axis=decision.axis,
                            trigger=(result.failure or Failure.INCONCLUSIVE).value,
                            because=list(decision.because), skipped=list(decision.skipped),
                            **({"standard": decision.standard} if decision.standard else {}),
                            turns_from=turns, turns_to=decision.max_turns if decision.continues else None,
                            spent_usd=round(report.cost_usd, 4),
                            **(_continuation(decision, resume, fresh) if decision.continues else {}),
                            **({"wait_s": decision.wait_s, "network": attempt.network} if attempt.network else {}),
                            **({"wait_s": decision.wait_s, "load": attempt.load} if attempt.load else {}))
            if not decision.continues:
                # The worker's own account goes with the reason: "the task is
                # wrong" is only actionable next to what it found wrong.
                report.status = _FINAL_STATUS[decision.move]
                report.summary = f"{decision.reason}. The last worker's own account: {result.summary}" if (
                    result.summary) else decision.reason
                report.stop = stops.Stop(
                    _cause(decision, attempt), decision.reason, denied=attempt.denied, allow=attempt.allow,
                    attempt=seq,
                    next_cell=decision.cell.label if decision.cell else None,
                    account=clip(findings_mod.text(attempt.blockers) if decision.move is Move.REVIEW
                                 else result.summary, 2000),
                    extra=({"checked": report.checked} if decision.move is Move.DECIDE
                           else _limit_extra(store, attempt) if _cause(decision, attempt) == "limit" else {})
                    | ({"widen": widen} if _cause(decision, attempt) in ("spec", "decision")
                       and (widen := _widen(store, task["id"], options.surfaces, result)) else {}))
                break
            if decision.move is Move.NEXT_MODEL and workspace is not None and carries_work(attempt):
                # Work locks' own check failed on is work, not a lost bet: the next model starts on it
                # instead of rebuilding it from the base (issue #124). A wrong approach, a repeated
                # answer or an independent check's blockers still start fresh.
                aside.append(f"Attempt {seq} ({cell.label}) left its changes in this worktree and locks' check "
                             "failed on them. Start from them: fix what the check shows, and undo only what is "
                             "wrong; do not rebuild what is right.")
                store.add_event(task["id"], "work_carried", seq=seq, to=decision.cell.label if decision.cell else "")
            elif decision.move is Move.NEXT_MODEL and workspace is not None:
                # A new model starts from the base, not the bet that lost; what the
                # last one did is kept on a ref, and the brief says both.
                ref = isolate.set_aside(workspace, seq)
                isolate.reset(workspace)
                if joined and joined["merged"]:
                    # The base is where the parts began: the ones that joined cleanly are joined again.
                    isolate.join(workspace, joined["merged"], message=_join_message(task["id"], options))
                if ref:
                    aside.append(f"Before this attempt the worktree was reset to the base commit {workspace.base[:12]}"
                                 f": attempt {seq}'s changes ({cell.label}) are not in it. They are kept: "
                                 f"`git diff {workspace.base[:12]} {ref}` shows them. Take what is right from them, "
                                 "not their approach. This reset is locks' own move to a new model, not a "
                                 "problem with the task: never report it as a spec or architecture point.")
            if decision.wait_s:
                mark = time.monotonic()
                _pause(store, task["id"], decision.wait_s, say,
                       f"the network dropped ({attempt.network}); trying {decision.cell.label} again"
                       if attempt.network else
                       f"the check's tests timed out under load ({attempt.load}); trying {decision.cell.label} again")
                timing["wait_s"] += time.monotonic() - mark
            cell, turns = decision.cell, decision.max_turns
    except Cancelled:
        report.stop = _cancelled(store, task["id"], len(attempts) or None)
        report.status, report.summary = "cancelled", report.stop.reason
    except KeyboardInterrupt:
        report.stop = stops.Stop("cancelled", "you cancelled it with Ctrl-C before it finished", by="you",
                                 attempt=len(attempts) or None, extra={"via": "keyboard"})
        report.status, report.summary = "cancelled", report.stop.reason
    finally:
        held.close()
        if workspace is not None:
            report.isolated = True
            if stopped := isolate.stop_stacks(workspace.path):
                store.add_event(task["id"], "stacks_stopped", projects=stopped)
            report.changed = list(isolate.changed(workspace))
            # Work that outgrew its turns is kept too: the task was not wrong, only big. A
            # cancel stops the work, never throws it away: what it changed stays on its branch.
            # Nor does a resume that ends short drop what an earlier run of it kept.
            outgrew = report.stop is not None and report.stop.cause in ("turns", "split")
            report.branch = isolate.finish(
                workspace, keep=report.status in KEEPS_WORK or outgrew or report.status == "cancelled" or resumed,
                message=isolate.message(task["id"], title=commit_title(task), subject=subject,
                                        summary=attempts[-1].summary if attempts else "",
                                        status="done" if report.status == "done" else report.status,
                                        check_output=len([p for p in report.check_output if p in report.changed])),
            )
            if report.branch:
                with contextlib.suppress(isolate.IsolationError):
                    report.commit = isolate.head(workspace.repo_dir, report.branch)
            if report.branch and report.status == "done":
                # The fix that worked is anchored to the commit a person can check.
                store.set_fix_commit(task["id"], isolate.head(workspace.repo_dir, report.branch))
        status = report.status if report.status != "running" else "failed"
        if status != "done" and report.stop is None:
            # Nothing above ended it: an error escaped the loop, and locks, not the work, failed.
            error = sys.exc_info()[1]
            report.stop = stops.Stop("crashed", f"locks itself failed mid-run: {type(error).__name__}: {error}"[:500]
                                     if error else "it ended without a verdict", attempt=len(attempts) or None)
            report.summary = report.summary or report.stop.reason
        store.update_task(task["id"], status=status, final_cell=report.final_cell, result=report.summary,
                          pid=None, current_cell=None)
        if status == "done":
            # Its earlier copies that stopped waited on this work; it is done.
            report.superseded = stops.supersede(store, store.get_task(task["id"]))
            # A resumed task that had paused its lane passed: the queue behind it goes on.
            if store.release_lane(key, task["id"]) or report.superseded:
                dispatch.reopened(task, os.environ)
        # A person's cancel is their call on this task, not a broken state the
        # next one would start on: the queue behind it goes on.
        by_a_person = report.stop is not None and report.stop.cause == "cancelled" and report.stop.who == "you"
        # An investigator's stop is no broken state of the project: the person's queue goes on.
        reading_for_init = '"investigation"' in (store.get_task(task["id"]).get("options") or "")
        if status != "done" and store.get_task(task["id"])["dispatched"] and not by_a_person and not reading_for_init:
            store.pause_lane(key, f"task #{task['id']} ended {status}: {clip(report.summary, 200, one_line=True)}")
        if workspace is None:
            report.changed = sorted(changed)
            report.in_place = _changed_places(repo_dir, report.changed)
        if status == "done":
            # A security problem the worker named is an alert of its own, never only a note among others.
            report.alerts = notes.security_alerts(report.learned, attempts[-1].findings if attempts else ())
        size = _realized(store, task["id"], report, workspace, time.monotonic() - clock)
        drifted = specs_mod.drift(workspace.repo_dir if workspace else repo_dir) \
            if status == "done" and writes and report.changed and config.enabled("specs", os.environ) else None
        if drifted is not None:
            # The report a `locks run` prints is its ending: it offers the catch-up as the hooks' ending does.
            report.impact += [line.strip() for line in specs_mod.ending_lines(
                task, report.changed, drifted.data(),
                after=f"run:{task['workflow_run']}" if task.get("workflow_run") else f"task:{task['id']}")]
        store.add_event(task["id"], "finished", status=status, final_cell=report.final_cell,
                        cost_usd=round(report.cost_usd, 4), branch=report.branch, impact=report.impact,
                        tip=_tip(workspace, report.branch), base=workspace.base if workspace else None,
                        commit=report.commit, checked=report.checked, isolated=report.isolated,
                        **({"in_place": report.in_place} if report.in_place else {}), alerts=report.alerts,
                        **({"specs": drifted.data()} if drifted else {}),
                        changed=report.changed[:CHANGED_KEPT], changed_n=len(report.changed),
                        stop=report.stop.data() if report.stop else None,
                        risk=the_plan.risk.data(), points=the_plan.points.points, realized=size.points,
                        realized_why=list(size.reasons),
                        findings=list(attempts[-1].findings)[:30] if status == "done" and attempts else [],
                        wall_s=round(time.monotonic() - clock, 2), attempts=len(attempts),
                        at=round(time.time(), 3), **gaps,
                        **{k: round(v, 2) for k, v in timing.items()},
                        **({"ledger": settled.data()} if settled else {}),
                        **({"verdict": verdict_card(store, task["id"], report, options, verifying)}
                           if status == "done" else {}),
                        **({"parts": list(report.parts), "contract": report.contract}
                           if status == "done" and report.parts else {}))
        store.add_message(task["id"], "worker", report.text())
        # The next step, and the work queued after this one, start here, from the engine:
        # no session relays them. Advancing never fails the run that just ended, and never silently.
        try:
            workflows.advance(store, store.get_task(task["id"]), os.environ)
        except Exception as exc:
            store.add_event(task["id"], "workflow_error", error=str(exc)[:300])
            say(f"locks: #{task['id']} could not start the work that waits on it: {exc}")
        # What follows the ending, after its ending and the next step are out: filing what it
        # learned (a model call, about 5 s at the median) and the late independent check. No
        # session and no step waits on either; with `detach_after` the command does not either.
        late_check = deferred if status == "done" and deferred is not None and report.branch and report.commit \
            else None
        if late_check is not None:
            report.late_check = True

        checkout = workspace.repo_dir if workspace else repo_dir
        filing = status == "done" and options.keep_learned and config.enabled("notes", os.environ, options.notes)
        _after_ending(store, detach=options.detach_after, task=task, key=key, report=report, plan=the_plan,
                      checkout=checkout, commit=isolate.head(workspace.repo_dir, report.branch)
                      if workspace and report.branch and filing and report.learned else None,
                      notes=filing, engram=status == "done" and options.keep_learned
                      and config.enabled("engram_write", os.environ),
                      notes_ask=notes_ask, late_check=late_check, text=text, base=base_commit,
                      launcher=launcher, registry=registry, say=say)
    return report


#: The most of one criterion's evidence a verdict keeps.
CRITERION_EVIDENCE = 240


def verdict_card(store: Store, task_id: int, report: Report, options: Options, verifying: bool) -> dict[str, Any]:
    """What the passing work was held to, kept with its ending so the session that sent it
    reads the verdict instead of re-reading the code: each criterion `S#` the worker
    answered with its evidence, the repository's check that locks ran (never the worker's
    word), the independent check's outcome or why it did not run, and how many attempts it
    took. Read from locks' records and git only."""
    verified = store.last_event(task_id, "verified")
    if verified is not None:
        data = verified["data"] or {}
        independent = {"ran": True, "passed": bool(data.get("passed")), "blocking": int(data.get("blocking") or 0),
                       # CORRECT lines of the request's flows it left unanswered: no one checked them.
                       **({"unchecked": list(data["tracer_unanswered"])} if data.get("tracer_unanswered") else {})}
    elif (skipped := store.last_event(task_id, "verifier_sampled")) is not None:
        # Its cell's record earned skip-lot sampling (`locks.trust`), and this pass was not drawn.
        independent = {"ran": False, "sampled": True, "every": int((skipped["data"] or {}).get("every") or 1),
                       "why": str((skipped["data"] or {}).get("why") or "")}
    elif store.last_event(task_id, "verifier_deferred") is not None:
        # The late check (`late_verifier`): it runs after this ending, on the kept branch.
        independent = {"ran": False, "late": True,
                       "why": "it runs after this ending; a blocker it shows reopens the task as review"}
    elif options.reviewed_next:
        independent = {"ran": False, "why": "a blind review step of this workflow reads the change next"}
    elif not verifying:
        independent = {"ran": False, "why": "it changed nothing to check, or the verifier is off"}
    else:
        independent = {"ran": False, "why": f"its risk ({report.plan.risk.tier}) does not call for one"}
    return {
        "criteria": [{"id": str(c.get("id")), "met": bool(c.get("met")),
                      "evidence": " ".join(str(c.get("evidence") or "").split())[:CRITERION_EVIDENCE]}
                     for c in report.criteria if isinstance(c, Mapping)],
        "check": _check_card(options.verify, report.preexisting,
                             store.last_event(task_id, "check_repeated")) if options.verify else None,
        "independent": independent,
        "attempts": len(report.cells),
        **({"unchecked": f"the worker never ran its own check ({report.unchecked}); locks' check passed in its "
                         "place, so its criteria are its word alone"} if report.unchecked else {}),
    }


#: What a verdict card says of a check that fails only as it does at the base.
FAILING_AT_BASE = "failing at base, unchanged"


def _check_card(command: str, found: Mapping[str, Any] | None,
                repeated: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The verdict card's check: passed, or failing at the base too with the same failures
    (`Baseline.found`): it stood as a pass, and the card says which it was. Work that fixes
    a flake says how many runs it passed (`runs`); any other check ran once."""
    runs = {"runs": int((repeated.get("data") or {}).get("runs") or 1)} if repeated else {}
    if not found:
        return {"command": command, "passed": True, **runs}
    return {"command": command, "passed": True, **runs, "state": FAILING_AT_BASE, "base": str(found.get("base") or ""),
            "failures": int(found.get("n") or 0), "fixed": int(found.get("fixed") or 0)}


_STEP_TAG = re.compile(r"^\[[\w.-]+#\d+ [\w.-]+\]\s*")
_LEAD = re.compile(r"^[A-Z][^:\n]{0,40}:\s+")


def _tip(workspace: isolate.Workspace | None, branch: str | None) -> str | None:
    """The commit a kept branch ends on, kept with the ending: what builds on it can still
    find the work if the branch is ever deleted."""
    if workspace is None or not branch:
        return None
    try:
        return isolate.head(workspace.repo_dir, f"refs/heads/{branch}")
    except (isolate.IsolationError, OSError):
        return None


def commit_title(task: Mapping[str, Any]) -> str:
    """The fallback subject of a kept commit when the worker proposed none: the task's
    title, except for a workflow step, whose title is the step's ("[feature#19 build]
    Build it, with its tests"): its change is its goal, the first line of its text
    without the step's lead-in ("Build this feature: ")."""
    title = str(task.get("title") or "")
    if not _STEP_TAG.match(title):
        return title
    first = next((line.strip() for line in str(task.get("body") or "").splitlines() if line.strip()), "")
    return _LEAD.sub("", first) or _STEP_TAG.sub("", title)


def _realized(store: Store, task_id: int, report: Report, workspace: isolate.Workspace | None,
              wall_s: float) -> points_mod.Score:
    """The size the task turned out to be, from what git and its attempts measured: the files it
    changed, the lines its kept branch holds (none counted without one), every attempt's turns,
    and the wall time of this run and the ones before it."""
    attempts = store.attempts(task_id)
    lines = None
    if workspace is not None and report.branch:
        counted = isolate.numstat(workspace.repo_dir, workspace.base, report.branch)
        lines = sum(n for n, _ in counted) if counted is not None else None
    before = sum(float(e["data"].get("wall_s") or 0) for e in store.events_of(task_id, "finished"))
    return points_mod.realized(files=len(report.changed), lines=lines,
                               turns=sum(int(a["turns"] or 0) for a in attempts),
                               minutes=(before + wall_s) / 60, attempts=len(attempts))


def _join_message(task_id: int, options: Options) -> Callable[[str], str]:
    """The merge commit of each part an integrate step joins: the part it brings, as the history reads it."""
    names = {b["branch"]: b.get("part") or b["branch"] for b in (options.integrate or {}).get("branches") or ()}
    return lambda branch: isolate.message(task_id, title=f"Join the {names.get(branch, branch)} part",
                                          summary=f"Merges {branch}, the part the plan named {names.get(branch)}.")


def _integrate(store: Store, task_id: int, workspace: isolate.Workspace | None, options: Options, report: Report,
               context: list[str], say: Callable[[str], None],
               baseline: Baseline | None = None) -> tuple[dict[str, Any] | None, bool]:
    """Join an integrate step's branches in its worktree, in the plan's order. A clean join
    whose check (the run's `--verify`) passes, or that has none, passes with no worker; a
    conflict, or a check that fails on the joined work, goes to one, told what and why.
    (what was joined, whether a worker must go on); `report` holds the ending otherwise."""
    spec = options.integrate or {}
    branches = [b["branch"] for b in spec.get("branches") or () if b.get("branch")]
    names = {b["branch"]: b.get("part") or b["branch"] for b in spec.get("branches") or ()}
    if workspace is None:
        report.stop = stops.Stop("environment", "an integrate step joins the parts' branches in a worktree of its "
                                 "own, and this run has none (not a git checkout, or isolation is off)")
        report.status, report.summary = "blocked", report.stop.reason
        return None, False
    try:
        joined = isolate.join(workspace, branches, message=_join_message(task_id, options))
    except isolate.IsolationError as exc:
        report.stop = stops.Stop("environment", f"could not join the parts: {exc}"[:500])
        report.status, report.summary = "blocked", report.stop.reason
        return None, False
    store.add_event(task_id, "integrated", merged=joined["merged"], conflict=joined["conflict"],
                    files=joined["files"])
    clean = ", ".join(names[b] for b in joined["merged"]) or "nothing"
    contract = (("\n\nThe contract every part keeps, from the plan, verbatim (context: keep it):\n"
                 + "\n".join(f"> {line}" for line in spec["contract"].splitlines())) if spec.get("contract") else "")
    if joined["conflict"]:
        branch = joined["conflict"]
        rest = " then ".join(f"`git merge --no-edit {b}`" for b in joined["left"][1:])
        say(f"locks: #{task_id} joining {names[branch]} conflicts in {', '.join(joined['files'][:5])}")
        context.append(
            f"Joining the parts in the plan's order: {clean} merged cleanly. Merging `{branch}` (the {names[branch]} "
            f"part) conflicts in: {', '.join(joined['files'])}. Run `git merge --no-edit {branch}` here, resolve "
            "those files so the parts work together and keep the contract, and commit the merge"
            + (f"; then merge the rest in this order, resolving each the same way: {rest}." if rest else ".")
            + contract)
        return joined, True
    summary = f"joined {len(joined['merged'])} parts cleanly ({clean})"
    if not options.verify:
        report.status, report.summary = "done", summary + "; no check was given: the review that follows reads it"
        return joined, False
    code, full = _steady_check(options.verify, workspace.path, baseline)
    output = full[-CHECK_OUTPUT_KEPT:]
    if code == 0:
        report.status, report.summary = "done", f"{summary}; `{options.verify}` passed on the joined work"
        return joined, False
    if (why := cannot_run(code, output)) is not None:
        report.stop = stops.Stop("environment", f"{summary}, but `{options.verify}` could not run here ({why})")
        report.status, report.summary = "blocked", report.stop.reason
        return joined, False
    before, compared = _against_base(baseline, options.verify, full, workspace.path) \
        if _ran_and_failed(code, full) else (False, "")
    if before:
        report.preexisting = baseline.found if baseline is not None else None
        report.status, report.summary = "done", (f"{summary}; `{options.verify}` fails on the joined work only as it "
                                                 "does at the base")
        return joined, False
    context.append(f"The parts merged cleanly ({clean}), but the run's check `{options.verify}` fails on the joined "
                   "work. Make it pass where the parts meet, keeping the contract; change what a part does only as "
                   f"far as that needs. Its output:\n{output[-1500:]}" + (f"\n\n{compared}" if compared else "")
                   + contract)
    return joined, True


#: Words that say a task needs a browser, or servers that keep running.
_BROWSER = re.compile(r"\b(browser|navegador|navigate|navega\w*|abr[ie]\w* (?:el )?navegador|screenshot|"
                      r"captura de pantalla|playwright|puppeteer|e2e)\b", re.IGNORECASE)
_SERVER = re.compile(r"(\bdev servers?\b|\blevanta\w*\b|\bstart (?:both |the )?(?:dev )?servers?\b|"
                     r"\bng serve\b|\bnpm (?:run )?(?:start|dev)\b|localhost:\d+)", re.IGNORECASE)
_BROWSER_CAPS = ("browser", "playwright", "chrome", "puppeteer", "rastro")
#: A task that tells the worker to start its servers: it needs them, whatever its check, and
#: its worker starts them itself.
_START_SERVERS = re.compile(
    r"(\blevanta\w*\b|\barranca\w*\s+(?:el|la|los|las|ambos)\b|"
    r"\binicia\w*\s+(?:el |la |los |las )?(?:servidor\w*|servicios?|backend|front\w*|api|app)\b|"
    r"\b(?:start|run|spin up|bring up|boot)\s+(?:up\s+)?(?:both |the |all |your |its )?(?:own )?(?:dev |local )?"
    r"(?:servers?|backend|back-?end|frontend|front-?end|front)\b"
    r"(?!\s+(?:tests?|checks?|suites?|specs?|lint\w*|builds?|unit\w*|pruebas?)\b))", re.IGNORECASE)
#: A check that talks to a running server: only then does a task's mention of one
#: say the check needs it. Lint, unit tests and a build do not.
_CHECK_SERVES = re.compile(r"(\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b|\be2e|playwright|cypress|puppeteer|\bcurl\b|"
                           r"\bwget\b|lighthouse|wait-on|start-server-and-test)", re.IGNORECASE)
#: A permission a check is run with (`--allow-net=localhost:8000`): no server it talks to.
_CHECK_GRANTS = re.compile(r"--allow-[\w-]+=\S*", re.IGNORECASE)


#: A task that says in its own words it must change nothing: a validation whose
#: findings are its answer, whatever kind it was filed under. The words must be
#: about the task: "a read-only version history" is a thing to build, and "do not
#: edit any file under src/admin" bounds a change instead of forbidding one.
_READ_ONLY = re.compile(
    r"\bread[- ]only(?:\s*[:)]|\s+(?:validation|audit|review|investigation|check|pass|run|session|task)\b)"
    r"|(?:tarea|validaci[oó]n|auditor[ií]a|revisi[oó]n|investigaci[oó]n|sesi[oó]n)\s+(?:de\s+)?s[oó]lo\s+"
    r"(?:de\s+)?lectura|[,(:]\s*s[oó]lo\s+(?:de\s+)?lectura\s*(?:[).,;\n]|$)"
    r"|\bdo not (?:edit|modify|change|touch)\b[\w ,]{0,25}?\bany (?:repo |repository )?files?\b"
    r"(?!\s+(?:under|inside|outside|other|except|besides|but|in\s+(?!any\b|the\s+repo|this\s+repo)))"
    r"|\bno (?:edites|modifiques|cambies|toques)\b[\w ,]{0,25}?\b(?:ning[uú]n\s+)?archivos?\b"
    r"(?!\s+(?:de|en|bajo|dentro|fuera|salvo|excepto|que)\b)",
    re.IGNORECASE)
#: What a task quotes or puts in parentheses is usually evidence about something else (what another
#: task did: "read-only", (a read-only review)). A parenthesis that opens with the declaration is the
#: task's own, whatever follows it: "Investigate (read-only) a timezone bug", "(read-only: report the
#: cause)", and the same in Spanish ("solo lectura").
_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d|`[^`\n]*`')
_ASIDE = re.compile(r"\((?!\s*(?:read[- ]only|s[oó]lo\s+(?:de\s+)?lectura)\s*[:;,)])[^()\n]*\)", re.IGNORECASE)
#: Kinds whose work is the change itself: whatever their text says is read-only,
#: they write. Every misreading of a task as read-only was one of these.
_BUILDS = frozenset({"feature", "implement", "refactor", "docs"})


def declares_read_only(kind: str | None, text: str) -> bool:
    """Whether the task says in its own words that it changes nothing. A build kind never does, and what
    the text quotes or puts in an aside describes something else, so it is not read."""
    if kind in _BUILDS:
        return False
    return bool(_READ_ONLY.search(_ASIDE.sub(" ", _QUOTED.sub(" ", text))))


_PORT = re.compile(r"(?:localhost|127\.0\.0\.1):(\d{2,5})\b")


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _all_listening(text: str) -> bool:
    """A task that names its local servers by port, every one of them up: the
    servers it needs are running, and saying otherwise is noise."""
    ports = {int(p) for p in _PORT.findall(text) if 0 < int(p) < 65536}
    return bool(ports) and all(_listening(p) for p in ports)


def _unmet(text: str, registry: Mapping[str, caps.Capability], *, served: bool = False,
           verify: str | None = None, where: Path | None = None) -> list[str]:
    """What a task asks for that a one-shot worker does not have, said before any
    money is spent: a worker has no browser unless a capability gives it one, or
    its check or the project's own tests drive a headless one (`ci.browser`), and
    a server it starts ends with it. A task that tells its worker to start its servers
    is said to have them started by its worker, never sent to `--service`; a task whose
    check needs no server (lint, tests, a build) is not told to start one for a word in
    its text, a host and port it names as context included."""
    notes = []
    if _BROWSER.search(text) and not any(k in name.lower() for name in registry for k in _BROWSER_CAPS):
        driven = ci.browser(where, verify) if where is not None else None
        if driven:
            notes.append(f"the browser this task needs is driven headless: {driven}")
        else:
            notes.append("this task needs a browser and workers have none: register a browser MCP server in "
                         "your locks capabilities.json (`locks capabilities --example` has one) and allow its "
                         "tools (--allow 'mcp__browser'), or check the pages yourself")
    mention, started = _SERVER.search(text), _START_SERVERS.search(text)
    if started and not served:
        notes.append(f"this task tells its worker to start its servers ({started.group(0).strip()!r}): the worker "
                     "starts them itself, and they stop when it finishes")
    elif mention and not served and not _all_listening(text) and (
            not verify or _CHECK_SERVES.search(_CHECK_GRANTS.sub(" ", verify))):
        notes.append(f"this task mentions {mention.group(0)!r}: if its work needs running servers, pass them with "
                     "--service 'npm run dev@4200' (locks starts each before the work and stops it after), or "
                     "start them yourself before dispatching; a server a worker starts stops when it finishes")
    return notes


def _unmeasured(kind: str, paths: Sequence[str], registry: Mapping[str, caps.Capability]) -> str:
    """Said when a change to the interface will be checked with nothing that measures a rendered
    page, and why: the `plugins` setting is off, or no such neighbour is installed. Empty when
    the work is no UI or something that measures it is in the registry."""
    if kind != "ui" and not any(caps.matches(path, caps.UI_FILES) for path in paths):
        return ""
    if any(c.measures and c.touches for c in registry.values()):
        return ""
    lacking = plugins.without_browser(os.environ) if config.enabled("plugins", os.environ) else []
    why = ("the `plugins` setting is off (`locks config plugins on`)" if not config.enabled("plugins", os.environ)
           else f"{', '.join(lacking)} is installed but this machine has no Chromium for it, so it is not "
                f"handed (`{plugins.BROWSER_FIX}`, or `LAYOUT_INSPECTOR_CHROMIUM` naming one)" if lacking
           else "the layout-inspector plugin is not installed and `layout-inspector-mcp` is not on PATH (install it: "
                 "`claude plugin install layout-inspector@rixmerz`, then `playwright install chromium` in its "
                 "environment, as its README says)")
    return (f"layout-inspector: this change touches the interface and nothing in your registry measures a rendered "
            f"page, so the independent check judges it by reading: {why}")


def _beside_ci(repo_dir: Path, check: str, services: Sequence[str], quieted: Collection[str] = ()) -> list[str]:
    """The jobs of the repository's CI the task's check leaves out, in one note (a warning:
    a pass here is not CI's), and the workflow files that could not be read, said once.
    Never a verdict and never a blocker; a job the person quieted here is not named."""
    note, unreadable = ci.left_out(repo_dir, check, services, quieted)
    lines = [note] if note else []
    if unreadable:
        lines.append(f"CI: {', '.join(unreadable)} could not be read; it is left out of what this check is "
                     "compared with")
    return lines


def _continues(decision: Decision, session: WorkerSession | None) -> tuple[str, str]:
    """The session the next attempt continues, and, when one was due and it starts fresh,
    why. More turns on the same cell continue it; more effort on the same model continues
    a fork of it at the new effort, since the approach stood and the depth was missing.
    Any other move is a new bet: a new model must not inherit the old one's approach, and
    a retry is for a launch that broke, unless only its answer's form failed: then the same
    cell answers again in its own session (`Decision.reshape`). A worktree is reset to the base only for another
    model (and undone outside its surfaces only as a wrong approach), so the model tells it."""
    if session is None or decision.cell is None or not (
            decision.move in (Move.MORE_TURNS, Move.MORE_EFFORT) or decision.reshape):
        return "", ""
    before, model = session.attempt, models.pinned(decision.cell.model) or decision.cell.model
    if not session.id:
        return "", f"attempt {before} kept no session to continue"
    if session.model != model:
        return "", f"attempt {before} ran on {session.model}, this one runs on {model}"
    if (decision.move is Move.MORE_TURNS or decision.reshape) and session.cell != decision.cell:
        return "", f"attempt {before} ran at {session.cell.label}, this one at {decision.cell.label}"
    return session.id, ""


def _continuation(decision: Decision, resume: str, fresh: str) -> dict[str, object]:
    """What a `moved` event says of the next attempt's session: continued or not, the
    session it goes on in (a fork for more effort), and why one due starts fresh. The
    attempt's own `attempt_finished` says whether it did (`fresh`: the CLI refused it)."""
    out: dict[str, object] = {"continued": bool(resume)}
    if resume:
        out["forked_from" if decision.move is Move.MORE_EFFORT else "resumed"] = resume
    if fresh:
        out["fresh"] = fresh
    return out


def _spans(repo_dir: Path, add_dirs: Sequence[str]) -> str | None:
    """A task that reaches a sibling repository (`--add-dir`) outside the workspace
    of its own: said once, with the command that makes them one project. Never done
    for the person: a workspace moves what each repository kept."""
    from locks import workspaces

    own = repo.toplevel(repo_dir)
    tops = [t for t in (repo.toplevel(Path(d).expanduser()) for d in add_dirs) if t is not None and t != own]
    if own is None or not tops:
        return None
    folder = workspaces.of(own)
    apart = [t for t in tops if folder is None or workspaces.of(t) != folder]
    if not apart:
        return None
    named = [own, *apart] if folder is None else apart
    spans = (f"this task spans {len(apart) + 1} repositories ({', '.join(t.name for t in [own, *apart])}): tracked "
             "apart, each keeps its own notes and board")
    choice = ("making them one project is the person's choice, never locks': it moves what each repository kept "
              "(notes, tasks, rules) to that folder")
    if folder is None:
        # Only a folder that exists and holds exactly these: a workspace's folder holds only
        # its own repositories, and a path nobody made is no offer.
        parent = own.parent
        try:
            held = {c.resolve() for c in parent.iterdir() if (c / ".git").exists()}
        except OSError:
            held = set()
        if held != {t.resolve() for t in named} or parent == Path.home().resolve() or parent == Path("/"):
            return (f"{spans}; a workspace needs a folder that holds just them, which the person makes (or "
                    f"links them into) before `locks workspace add <folder> --with …`; {choice}")
        folder = parent
    flags = " ".join(f"--with {shlex.quote(str(t))}" for t in named)
    return (f"{spans}; `locks workspace add {shlex.quote(str(folder))} {flags}` would make them one project, "
            f"tracked in that folder; {choice}")


#: The repository's latest attempts a refusal is read from: an older one is history, not a warning.
REFUSALS_WINDOW = 40
#: The tasks a rule must have been refused in, within that window, before a plan names it.
REFUSED_IN_TASKS = 2


def _refused_here(store: Store, key: str | None, granted: Sequence[str], mode: str) -> list[tuple[str, int]]:
    """The rules workers in this repository lacked most often lately, past refusals read
    back to rules when an older run did not record them, the granted ones aside.

    Only refusals among the repository's latest `REFUSALS_WINDOW` attempts, of a rule
    refused in `REFUSED_IN_TASKS` tasks or more: one task's refusal is that task's, and
    is not repeated to every task after it. Only in the mode the next worker runs in:
    what `acceptEdits` refused, auto mode may well let through. An attempt that did not
    record its mode ran before auto mode, under `acceptEdits`."""
    if not key:
        return []
    counts: dict[str, int] = {}
    tasks: dict[str, set[int]] = {}
    for row in store.refusals(key, limit=REFUSALS_WINDOW):
        if (row.get("mode") or grants.FALLBACK_MODE) != mode:
            continue
        for rule in dict.fromkeys(row["allow"] or allow_rules.from_refusals(row["denied"])):
            if rule not in granted:
                counts[rule] = counts.get(rule, 0) + 1
                tasks.setdefault(rule, set()).add(int(row["task_id"]))
    shown = {rule: n for rule, n in counts.items() if len(tasks[rule]) >= REFUSED_IN_TASKS}
    return sorted(shown.items(), key=lambda kv: (-kv[1], kv[0]))[:8]


#: A task the plan sizes at this many points or more is several workers' work: out of turns,
#: it is split (`locks split`) instead of given more (`escalate.decide`'s `several`).
SPLIT_POINTS = 8


def _several(the_plan: Plan, options: Options, task: Mapping[str, Any]) -> str:
    """Why the plan sized this task as several workers' work, or "": a build of
    `SPLIT_POINTS` or more that the session did not send `--whole`, outside a workflow run
    (a step's parts are its plan's) and never a reading kind."""
    if (options.whole or task.get("workflow_run") or the_plan.kind in READ_ONLY_KINDS or options.parts
            or options.integrate or options.ledger or the_plan.points.points < SPLIT_POINTS):
        return ""
    return f"{the_plan.points.points} points: " + ", ".join(the_plan.points.reasons[:3])


def _limit_extra(store: Store, attempt: Attempt) -> dict[str, Any]:
    """A stop for the account's usage limit: when the limit resets, when the message named it,
    kept on the stop for the resume after it and on the store's meta for the dispatcher."""
    if attempt.retry_after_s is None:
        return {}
    at = (datetime.now(UTC) + timedelta(seconds=float(attempt.retry_after_s))).isoformat(timespec="seconds")
    with contextlib.suppress(sqlite3.Error):
        if (store.meta(stops.LIMIT_META) or "") < at:
            store.set_meta(stops.LIMIT_META, at)
    return {"resets_at": at}


#: The most paths outside its surfaces a stop offers to widen them by.
WIDEN_MAX = 5
_SURFACE_WORDS = re.compile(r"\b(surfaces?|superficies?|outside (my|the|its) (scope|allowed)|not allowed to "
                            r"(change|touch|edit)|may not (change|touch|edit))\b", re.IGNORECASE)


def _widen(store: Store, task_id: int, surfaces: Sequence[str], result: launch.WorkerResult) -> list[str]:
    """The paths a task that stopped on a point would need beyond its surfaces: the ones its
    attempts changed outside them (undone), and the ones its worker names when it says its
    surfaces kept it from them. Offered as a resume with them added (`stops.next_step`), so
    the diagnosis is not lost to a new task; never applied on its own."""
    if not surfaces:
        return []
    strayed = [str(p) for e in store.events_of(task_id, "outside_surfaces") for p in e["data"].get("paths") or ()]
    said = f"{result.summary}\n{result.evidence}"
    named = points_mod.named(said) if _SURFACE_WORDS.search(said) else []
    found = (p.strip("/") for p in [*strayed, *named])
    return list(dict.fromkeys(p for p in found if p and "*" not in p and not surfaces_mod.allows(surfaces, p)
                              and not surfaces_mod.dependency(p)))[:WIDEN_MAX]


def _blocked_cause(decision: Decision, attempt: Attempt) -> str:
    """What blocked the run, from the failure that led to it."""
    if attempt.failure is Failure.PERMISSION or attempt.denied:
        return "permission"
    if attempt.limited == "usage" and attempt.failure is Failure.ENVIRONMENT:
        return "limit"
    if attempt.base_unknown:
        return "base_unknown"
    if decision.stuck:
        return "same_failures"
    if attempt.network:
        return "network"
    return "load" if attempt.load else "environment"


def _cause(decision: Decision, attempt: Attempt) -> str:
    """What ended the run, from the move and the failure that led to it."""
    if decision.move is Move.BLOCKED:
        return _blocked_cause(decision, attempt)
    if decision.move is Move.NEEDS_APPROVAL:
        return "approval"
    if decision.move is Move.CONVERGED:
        return "converged"
    if decision.move is Move.REVIEW:
        return "review"
    if decision.move is Move.DECIDE:
        return "decision"
    if decision.move is Move.REPLAN:
        if attempt.failure in (Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG):
            return "spec"
        if decision.split:
            return "split"
        return "turns" if attempt.failure is Failure.TURNS_EXHAUSTED else "ladder"
    return "exhausted"


def _check_cancel(store: Store, task_id: int) -> None:
    if store.cancel_requested(task_id):
        raise Cancelled


#: What waits between tries; a test replaces it.
_sleep = time.sleep


def _pause(store: Store, task_id: int, seconds: int, say: Callable[[str], None], why: str) -> None:
    """Wait before the next try, said once, and stop at once for a cancel: a wait
    that says nothing reads as a run that hung."""
    say(f"locks: #{task_id} {why} in {seconds}s")
    store.add_event(task_id, "waiting", reason=why, seconds=seconds)
    left = float(seconds)
    while left > 0:
        _check_cancel(store, task_id)
        _sleep(min(left, 2.0))
        left -= 2.0
    _check_cancel(store, task_id)


def _waits(store: Store, task_id: int, who: str, say: Callable[[str], None]) -> None:
    """Said once, when a run finds its checkout taken: a wait that says nothing
    reads as a run that hung."""
    say(f"locks: #{task_id} waits: {who} is working in this checkout without a worktree; to work beside it, "
        "run it in its own worktree (without --no-isolate) — a wait is not a hang, do not cancel it for one")
    store.add_event(task_id, "waiting", reason=f"{who} is working in this checkout")


def _cancelled(store: Store, task_id: int, attempt: int | None) -> stops.Stop:
    """A cancel a person asked for says how they asked; a SIGTERM nobody asked
    locks for (a closed terminal, a killed process) says that instead."""
    if store.cancel_requested(task_id):
        asked = store.last_event(task_id, "cancel_requested")
        via = str(((asked or {}).get("data") or {}).get("via") or "cli")
        how = stops.CANCEL_VIA.get(via, via)
        return stops.Stop("cancelled", f"you cancelled it {how} before it finished", by="you", attempt=attempt,
                          extra={"via": via})
    return stops.Stop("signal", "a SIGTERM that did not come from `locks cancel` or the UI stopped it "
                      "(a closed terminal, or the process was killed)", attempt=attempt)


def how_to_run(workdir: Path, rules: Sequence[str]) -> list[str]:
    """Brief lines on running the project's tools: the Node already on the worker's
    PATH, and the commands its settings let through, in the forms that pass. Workers
    that sourced a version manager or wrapped a command in a shell were refused."""
    lines = []
    node = runtime.node_bin(workdir)
    if node is not None:
        lines.append(f"Node {node.version} (what {node.source} asks for) is already first on your PATH: run "
                     "`npm test`, `npx tsc` and the project's scripts by name. Never `source ~/.nvm/nvm.sh`, "
                     "`nvm use` or `bash -c '…'`: they are refused, and a refused command stops the task.")
    allowed = [r for r in rules if r.startswith("Bash(")]
    if allowed:
        lines.append("Shell commands your settings let through without asking, in these forms only, one per "
                     "call: " + ", ".join(dict.fromkeys(allowed[:20])))
    return lines


#: Fewer corrections than this a verifier's findings may earn; then a person decides.
MAX_CORRECTIONS = 2


def _gaps(store: Store, task_id: int, began: float) -> dict[str, float]:
    """What came before a dispatched run of this task: `advance_s` (from the step it waited
    on passing to its being queued), `pickup_s` (from queued, or released from behind other
    work, to the dispatcher starting it) and `spawn_s` (from that to this run's start). Only
    what this run's own events measured, since its last ending; nothing is guessed."""
    ended = store.last_event(task_id, "finished")
    since = ended["id"] if ended else 0
    out: dict[str, float] = {}
    advanced = store.last_event(task_id, "advanced")
    took = ((advanced or {}).get("data") or {}).get("advance_s")
    if advanced and advanced["id"] > since and isinstance(took, int | float):
        out["advance_s"] = round(float(took), 3)
    dispatched = store.last_event(task_id, "dispatched")
    at = ((dispatched or {}).get("data") or {}).get("at")
    if not dispatched or dispatched["id"] <= since or not isinstance(at, int | float):
        return out
    out["spawn_s"] = round(max(began - at, 0.0), 3)
    released = store.last_event(task_id, "released")
    task = store.get_task(task_id) or {}
    queued = _epoch(released["ts"] if released and released["id"] > since else task.get("created_at"))
    if queued is not None:
        out["pickup_s"] = round(max(at - queued, 0.0), 3)
        out.update(pickup_causes(store, task_id, queued, at, dispatched["data"] or {}))
    return out


#: What a task's pickup waited on, each a key of its `finished` event: work ahead it was judged
#: to wait for, the account's usage limit, the person's ceiling of workers, the machine's level.
PICKUP_CAUSES = ("pickup_conflict_s", "pickup_limit_s", "pickup_ceiling_s", "pickup_level_s")


def pickup_causes(store: Store, task_id: int, queued: float, at: float, dispatched: Mapping[str, Any]
                  ) -> dict[str, float]:
    """Its pickup (`queued` to `at`, epoch seconds) split by what it waited on, from its own
    records: the spans a dispatch decision said wait (`dispatch_decided`, until the next one),
    the spans a usage limit held the queue (`limit_waiting`), and the rest a worker: the
    person's ceiling when the level stood at it (`dispatched.ceiling`, a choice), else the
    machine's level. The parts add up to the pickup; nothing is guessed."""
    whole = max(at - queued, 0.0)
    if not whole:
        return {}
    spans: dict[str, float] = {"pickup_conflict_s": 0.0, "pickup_limit_s": 0.0}
    held_from: float | None = None
    for event in store.events_of(task_id, "dispatch_decided"):
        when = _epoch(event["ts"])
        if when is None or when >= at:
            continue
        if held_from is not None:
            spans["pickup_conflict_s"] += max(min(when, at) - max(held_from, queued), 0.0)
            held_from = None
        if not event["data"].get("parallel"):
            held_from = when
    if held_from is not None:
        spans["pickup_conflict_s"] += max(at - max(held_from, queued), 0.0)
    for event in store.events_of(task_id, "limit_waiting"):
        when, until = _epoch(event["ts"]), event["data"].get("until")
        if when is not None and isinstance(until, int | float):
            spans["pickup_limit_s"] += max(min(float(until), at) - max(when, queued), 0.0)
    known = min(spans["pickup_conflict_s"] + spans["pickup_limit_s"], whole)
    rest = whole - known
    ceiling, level = dispatched.get("ceiling"), dispatched.get("level")
    chosen = isinstance(ceiling, int) and isinstance(level, int) and level >= ceiling
    spans["pickup_ceiling_s" if chosen else "pickup_level_s"] = rest
    return {k: round(v, 3) for k, v in spans.items() if v}


def _epoch(stamp: str | None) -> float | None:
    """A stored timestamp as seconds since the epoch; None when it cannot be read."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return (then if then.tzinfo else then.replace(tzinfo=UTC)).timestamp()


def _seconds_since(stamp: str | None) -> float | None:
    """Seconds from a stored timestamp to now; None when it cannot be read."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return round(max((datetime.now(UTC) - then).total_seconds(), 0.0), 1)


def _session_context(task: Mapping[str, object], text: str) -> tuple[list[str], dict[str, int] | None]:
    """The part of the sending session's thread this task needs, as the scribe kept
    it, read up to now (the turn that sent it is not over). An investigator gets
    none: its brief is the survey. Reading it never fails the run."""
    session_id = str(task.get("session_id") or "")
    if not session_id or '"investigation"' in str(task.get("options") or "") or not config.enabled(
            "scribe", os.environ):
        return [], None
    from locks import scribe

    try:
        state = scribe.load(home(), session_id)
        transcript = (state or {}).get("transcript") or scribe.find_transcript(session_id)
        if transcript:
            state = scribe.update(home(), session_id, transcript) or state
        lines, got = scribe.context_for(state, text)
    except (OSError, ValueError, KeyError, TypeError):
        return [], None
    return lines, (got if lines else None)


def _twins(store: Store, task: Mapping[str, object], repo_dir: Path) -> list[int]:
    """Earlier tasks here with the same text that stopped and keep their work on a branch."""
    if repo.toplevel(repo_dir) is None:
        return []
    body = (task.get("body") or "").strip()
    return [t["id"] for t in store.list_tasks(repo=task.get("repo"), status=list(stops.DISMISSABLE),
                                              source=("locks", "queue"), limit=200)
            if t["id"] != task["id"] and (t.get("body") or "").strip() == body and isolate.kept(repo_dir, t["id"])]


def _fill_worktree(store: Store, task_id: int, workspace: isolate.Workspace, text: str, carry: bool,
                   say: Callable[[str], None]) -> list[str]:
    """What a new worktree lacks that the task needs, from the person's checkout: their
    uncommitted changes (brought with `carry`, else warned about when the task names
    them), and the ignored paths the task names (copied). Brief lines for each."""
    lines = []
    dirty = isolate.dirty(workspace.repo_dir)
    named = isolate.named_in(text, dirty)
    if carry and dirty:
        carried = isolate.carry(workspace, dirty)
        store.add_event(task_id, "carried", paths=carried[:100])
        say(f"locks: #{task_id} brought {len(carried)} uncommitted change(s) from your checkout into its worktree")
        lines.append("The person's uncommitted changes were brought into this worktree, as a starting point: "
                     + ", ".join(carried[:15]) + (f" and {len(carried) - 15} more" if len(carried) > 15 else ""))
    elif named:
        store.add_event(task_id, "uncommitted", paths=named[:100])
        say(f"locks: #{task_id} warning: {', '.join(named[:5])} have uncommitted changes in your checkout that "
            "its worktree does not have; commit them, or run it with --carry to bring them")
        lines.append("These files have uncommitted changes in the person's checkout that are not in this worktree: "
                     + ", ".join(named[:15]) + ". If the task needs that work, say so (`environment`) rather than "
                     "recreating it.")
    copied, big = isolate.bring(workspace, isolate.ignored_named(workspace, text))
    if copied:
        lines.append("Copied from the person's checkout (ignored by git, so never committed): " + ", ".join(copied))
    if big:
        lines.append("Named by the task but ignored by git and too big to copy here: " + ", ".join(big)
                     + ". If the task needs them, say so (`environment`).")
    return lines


def in_place_checkouts(repo_dir: Path) -> list[Path]:
    """The checkouts a run with no worktree in `repo_dir` may change: its own, or, in a folder that
    is no checkout, the ones inside it (the ones `launch.tracked_state` reads)."""
    top = repo.toplevel(repo_dir)
    return [top] if top is not None else launch.nested_checkouts(repo_dir)


def _changed_places(repo_dir: Path, changed: Sequence[str]) -> list[str]:
    """The checkouts inside a folder that is none that a run with no worktree changed files in, by
    name; [] in a checkout, whose one place needs no name."""
    if repo.toplevel(repo_dir) is not None:
        return []
    names = [c.relative_to(repo_dir).as_posix() for c in launch.nested_checkouts(repo_dir)]
    return [n for n in in_place_files(names, changed) if n != "(the folder)"]


def _base_places(folder: Path) -> tuple[tuple[str, str], ...]:
    """Each repository inside a folder that is no checkout, by its path from the folder, with the
    commit it is at: what a check run from that folder is compared at. A repository with no commit
    yet has no base to run at and is left out; () in a checkout, which has one commit."""
    if repo.toplevel(folder) is not None:
        return ()
    found = ((c.relative_to(folder).as_posix(), _head(c)) for c in launch.nested_checkouts(folder))
    return tuple((name, commit) for name, commit in found if commit)


def _head(repo_dir: Path) -> str | None:
    try:
        return isolate.head(repo_dir, "HEAD")
    except isolate.IsolationError:
        return None


def _cheap(cell: Cell) -> bool:
    """A cell whose pass a medium-risk change does not take on its own word: Haiku, or a model below
    Opus at low effort. Opus at low effort passed every hidden test of `bench/speed/versus.py`'s builds
    on its own, and checked as cheap its medium changes climbed to opus/high for nothing."""
    return cell.model == "haiku" or (cell.effort == "low" and cell.model != "opus")


def verify_cell(cell: Cell) -> Cell:
    """The verifier runs on the work's model (never Haiku) at high effort. Its
    independence is a fresh session given the request verbatim, not effort: work
    that climbed to xhigh or max is checked at high, which halves the wait on the
    slowest tasks and finds what a probe shows as well."""
    if cell.model == "haiku":
        return Cell("sonnet", "high")
    if cell.effort is not None and cell.effort != "high":
        return Cell(cell.model, "high")
    return cell


#: The turns of one independent check: it reads a diff and runs probes, and a
#: worker that climbed to many turns does not hand it the same room.
VERIFIER_TURNS = 40
#: Kinds whose change must make a failure go away: the check shows it at the base, and gone.
_REPRODUCES = frozenset({"debug-repro", "debug-unclear"})


def kept_evidence(text: str, room: int = 4000) -> str:
    """What an attempt's record keeps of its evidence: its start and its end, so a
    resume's brief that quotes the last lines quotes the real ones."""
    if len(text) <= room:
        return text
    half = (room - 3) // 2
    return f"{text[:half]}\n…\n{text[-half:]}"


def verifier_brief(text: str, kind: str, workdir: Path, base: str | None, changed: Sequence[str],
                   known: Sequence[Mapping[str, str]] = (), why: Sequence[str] = (),
                   rules: Sequence[Mapping[str, Any]] = (), rows: Sequence[str] = (), running: str = "",
                   impact: Sequence[str] = (), method: Sequence[str] = (),
                   since: tuple[str, Sequence[str]] | None = None) -> str:
    """What the verifier is given: the request in its words, the change, why it is checked
    (the risk's reasons, in the router's words) and the constraints the project keeps —
    never the worker's account."""
    shown = ", ".join(changed[:20]) + (f" and {len(changed) - 20} more" if len(changed) > 20 else "")
    parts = [f"Check this change. The request it answers ({kind}), verbatim:\n{text}"]
    if listed := criteria_mod.brief(text):
        parts.append(listed)
    if why:
        parts.append("Why this check runs: " + "; ".join(why) + ". Probe those first.")
    if kind in _REPRODUCES and base:
        parts.append(f"This change fixes a failure: show it at the base commit {base[:12]} (`git show "
                     f"{base[:12]}:<path>`, or a temporary `git worktree add` of it outside the project that you "
                     "remove after; never `git stash`), then show it is gone on the change.")
    where = (f"The change is in {workdir}: `git diff {base}` shows it against the base commit {base}, the state "
             "before it." if base else f"The change is in {workdir}, uncommitted: `git diff` shows it.")
    parts.append(f"{where} Files it changed: {shown}.")
    links = [line.removeprefix(specs_mod.LINK_PREFIX) for line in impact if line.startswith(specs_mod.LINK_PREFIX)]
    if reach := [line for line in impact if not line.startswith(specs_mod.LINK_PREFIX)]:
        # What the code index (livespec) says the change reaches beyond its own files: where to probe.
        parts.append("What the code index says this change reaches, and what a plan before it said could go wrong "
                     "(data, not findings: probe what "
                     "matters):\n" + "\n".join(f"- {line}" for line in reach))
    if links:
        parts.append(specs_mod.LINK_CHECK + "\nlocks found these without the comment:\n"
                     + "\n".join(f"- {line}" for line in links))
    if known:
        parts.append("The previous check found these; say for each whether it is fixed, and report any new "
                     "problem apart:\n" + findings_mod.text(known))
    if since is not None:
        parts.append(recheck_lines(*since))
    if lines := notes.constraint_lines(rules):
        parts.append("\n".join(lines) + "\nA change that breaks one that applies to it is a finding, shown with "
                     "the case that breaks it.")
    if method and kind not in READ_ONLY_KINDS:
        parts += method
    if rows:
        # A fix of ledger rows: its answers per row decide the re-check after it (`workflows._ledger`).
        parts.append(f"The request is a fix of the ledger rows {', '.join(rows)}. Answer every one in `verdicts`, "
                     f"one entry per id, with `verdict` one of {', '.join(ledger_mod.WORDS['recheck'])} and the "
                     "`proof` that shows it: `fixed` only when you can show it in the change, in the code the row "
                     "is about; a row only written down (a note, a doc, an instruction file) or answered by "
                     "changing a test or its data is `not_fixed`; `regression` when the fix broke something at "
                     "or around it. Report anything else the change broke in `findings`.")
    if running:
        # It runs beside locks' own check (`Ahead`): one run of the suite, not two at once in one directory.
        parts.append(f"locks runs the project's check, `{running}`, in this directory now, beside you, and its "
                     "result decides whether the work passes: do not run it yourself. Probe with narrower "
                     "commands: one test, one file, a short script.")
    parts.append("This check is read-only: change no file of the project.")
    return "\n\n".join(parts)


def _tolerating(ahead: Ahead | None, wrote: Sequence[str]) -> Ahead | None:
    if ahead is not None:
        ahead.tolerated = tuple(wrote)
    return ahead


def _awaited(store: Store, task_id: int) -> bool:
    """Whether queued work waits on this task (`--after`): what its pass releases could not be held
    back by a blocker found after it."""
    return any(after.hold(t) == {"task": task_id} for t in store.queued_everywhere())


def _follow_ending(store: Store, *, task: dict, key: str | None, report: Report, plan: Plan, checkout: Path,
                   commit: str | None, notes: bool, engram: bool, notes_ask: notes_mod.Ask | None,
                   late_check: Mapping[str, Any] | None, text: str, base: str | None, launcher: Launcher,
                   registry: Mapping[str, caps.Capability] | None, say: Callable[[str], None]) -> None:
    """What follows a run's ending: filing what its worker learned (a model call, about 5 s at the
    median, timed apart by a `notes_filed` event), giving it to engram, and the late independent
    check. No session and no step waits on any of it."""
    mark = time.monotonic()
    if notes and report.learned:
        _note(store, task, key, report, plan, checkout, commit, notes_ask)
    if notes and report.constraints and key:
        kept = notes_mod.file_constraints(store, key, report.constraints, author="worker",
                                          source=f"task #{task['id']}", task_id=task["id"])
        if kept:
            report.noted = [*report.noted, *kept]
            store.add_event(task["id"], "noted", notes=kept, constraints=True)
    if engram and report.learned:
        _to_engram(store, task, report, plan, checkout)
    if notes and report.learned:
        store.add_event(task["id"], "notes_filed", notes_s=round(time.monotonic() - mark, 2))
    if late_check is not None and report.commit:
        _late_check(store, task["id"], text=text, kind=plan.kind, risk=plan.risk, repo_dir=checkout, base=base,
                    commit=report.commit, deferred=late_check, launcher=launcher, registry=registry, say=say)


#: Starts the detached process (a test passes its own).
_spawn = subprocess.Popen


def _quiet(_: str) -> None:
    """What a detached process says: nothing, its output is closed."""


def _after_ending(store: Store, *, detach: bool, **work: Any) -> None:
    """Run `_follow_ending`: in a fresh process when `detach` (the command that started the run
    returns at once), else here. Fresh, never a fork: a SQLite connection does not survive one,
    and a forked child's writes were lost when it closed. Its arguments go in a file in the locks
    home that the process reads and removes; work that cannot be written so runs here."""
    if detach:
        name = ""
        try:
            spool = home() / "work"
            spool.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="after-", suffix=".pickle", dir=spool)
            with os.fdopen(fd, "wb") as fh:
                pickle.dump({**work, "say": _quiet}, fh)
            source = str(Path(__file__).resolve().parents[1])
            code = (f"import sys; sys.path.insert(0, {source!r}); "
                    f"from locks.orchestrate import _detached; _detached({name!r})")
            _spawn([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, start_new_session=True, env=dict(os.environ))
            return
        except Exception:  # what cannot be detached runs here, as before
            with contextlib.suppress(Exception):
                os.unlink(name)
    _follow_ending(store, **work)


def _detached(path: str) -> None:
    """The detached process `_after_ending` starts: read its work, remove the file, run it on a
    store of its own. An error is kept where the next SessionStart reports it."""
    try:
        with open(path, "rb") as fh:
            work = pickle.load(fh)  # noqa: S301 - written by this locks, in the person's own locks home
        os.unlink(path)
        store = Store.open()
        try:
            _follow_ending(store, **work)
        finally:
            store.close()
    except BaseException:
        with contextlib.suppress(Exception):
            from locks import hooks
            hooks.note_error(home(), "after-ending")


def _late_check(store: Store, task_id: int, *, text: str, kind: str, risk: risk_mod.Risk, repo_dir: Path,
                base: str | None, commit: str, deferred: Mapping[str, Any], launcher: Launcher,
                registry: Mapping[str, caps.Capability] | None, say: Callable[[str], None]) -> None:
    """The independent check of a pass that ended on locks' own check (`late_verifier`): read on the
    branch it kept, in a temporary worktree at its tip. A blocker shown reopens the task as `review`,
    its work kept on its branch, and its session hears of it as a new ending; `locks resume` gives
    its first check those blockers. A check that cannot decide leaves the pass standing."""
    try:
        with isolate.at_base(repo_dir, commit, home()) as where:
            spec = dataclasses.replace(deferred["spec"], launch_dir=where, workdir=None)
            check, vcell = _check_spec(text=text, kind=kind, cell=deferred["cell"], spec=spec, workdir=where,
                                       base=base, changed=deferred["changed"], risk=risk, ledger={},
                                       registry=registry, rules=deferred["rules"], rows=(),
                                       impact=deferred.get("impact") or (), index=deferred.get("index") or None)
            mark = time.monotonic()
            verdict = launcher(check)
            wall_s = time.monotonic() - mark
    except BaseException as exc:  # a detached check that stops on a signal still says so
        store.add_event(task_id, "verified", seq=deferred["nth"], late=True, passed=None,
                        summary=f"the late independent check could not run: {exc!r}"[:500])
        if not isinstance(exc, Exception):
            raise
        return
    shown = findings_mod.read({"findings": list(verdict.findings)})
    blockers = findings_mod.blocking(shown)
    unmet_lines, unanswered = _correct_lines(verdict, dict(tracer_mod.criteria(repo_dir, text)[0])
                                             if config.enabled("tracer", os.environ) else {})
    blockers += [b for b in unmet_lines if not any(findings_mod.same(b, f) for f in blockers)]
    if not verdict.passed and not blockers and verdict.verdict == "fail":
        blockers = [{"where": "", "what": verdict.summary or "the check failed without a finding",
                     "evidence_class": "inferential", "cause": "unknown", "proof": ""}]
    store.add_event(task_id, "verified", seq=deferred["nth"], late=True, cell=vcell.label,
                    **({"tracer_unanswered": unanswered} if unanswered else {}),
                    worker=deferred["cell"].label, risk=risk.data(),
                    verdict=verdict.verdict, passed=not blockers and verdict.passed, findings=list(shown)[:20],
                    blocking=len(blockers), blockers=list(blockers)[:20], cost_usd=verdict.cost_usd,
                    summary=verdict.summary[:500], round=0, wall_s=round(wall_s, 2),
                    duration_s=round(verdict.duration_s, 2), turns=verdict.turns)
    task = store.get_task(task_id) or {}
    store.update_task(task_id, cost_usd=(task.get("cost_usd") or 0) + verdict.cost_usd)
    if not blockers or task.get("status") != "done":
        return
    say(f"locks: #{task_id} its late independent check found {len(blockers)} problem(s): it is open again")
    stops.record(store, task_id, stops.Stop(
        "review", f"its independent check, run after it ended, found {len(blockers)} problem(s) in the change",
        attempt=deferred["nth"], account=clip(findings_mod.text(blockers), 2000)))


def _impact(plan: Plan, changed: Sequence[str], repo_dir: Path, *, workdir: Path | None = None,
            base: str | None = None) -> list[str]:
    """What the adopted neighbours' indexes say a change reaches (livespec: the specs it touches,
    the callers outside it), for the independent check to probe, and the specs it wrote or changed
    that no code names in an `@spec:` comment, where workers were told to (`_links`). Never fails a run."""
    lines: list[str] = []
    for adapter, status in plan.active:
        try:
            lines += [x for x in adapter.assess(changed, repo_dir, status) if " could not " not in x]
        except Exception:  # noqa: S112 - an index that cannot say is no reason to skip the check
            continue
    return lines + list(plan.seen_risks) + _links(plan, changed, workdir, base)


def _later_notes(store: Store, key: str | None, the_plan: Plan, changed: Sequence[str]) -> list[str]:
    """The lines of the notes anchored to files the work changed that its first brief did not
    carry (or left out on trial). Never fails a run."""
    if not key or not changed or not config.enabled("notes", os.environ):
        return []
    try:
        found = notes.anchored_to(store, key, changed,
                                  skip=[*(int(n["id"]) for n in the_plan.notes), *the_plan.withheld])
    except sqlite3.Error:
        return []
    return notes.brief_lines(found) if found else []


def _livespec_calls(store: Store, task_id: int, nth: int, handed: Sequence[str]) -> dict[str, int] | None:
    """The livespec tools an attempt's worker called, by name and count: {} when it was handed livespec and
    called nothing, None when it was not handed it. The hooks' log is read into the store first, since a
    worker's calls are there only once it is. Never fails a run."""
    if "livespec" not in handed:
        return None
    with contextlib.suppress(OSError):
        habits.load_events(store)
    return store.tool_counts(task_id, nth, "mcp__livespec__")


def _unlinked(plan: Plan, changed: Sequence[str], workdir: Path | None,
              base: str | None) -> list[specs_mod.Requirement]:
    """The specs a change wrote or changed with no `@spec:` comment on any code: a check of what the
    brief required (`spec_links_brief`), so only where a worker was told to. Never fails a run."""
    if workdir is None or not config.enabled("specs", os.environ) or not any(
            hasattr(adapter, "spec_links_brief") for adapter, _ in plan.active):
        return []
    try:
        return specs_mod.unlinked(workdir, base, changed)
    except (OSError, ValueError):
        return []


def _links(plan: Plan, changed: Sequence[str], workdir: Path | None, base: str | None) -> list[str]:
    """`_unlinked` as lines: a spec written with none blocks; one that was there before is a warning."""
    return specs_mod.link_lines(_unlinked(plan, changed, workdir, base))


def _risk_of_change(plan: Plan, changed: Sequence[str] | None, workdir: Path | None, base: str | None) -> None:
    """The tier raised by what an attempt changed (`risk_mod.of_change`), and to high when it wrote a spec
    that no code links: the report calls that a blocker, and only the independent check can block."""
    risk_mod.of_change(plan.risk, changed)
    if changed and any(r.new for r in _unlinked(plan, changed, workdir, base)):
        plan.risk.raise_to("high", "it wrote a spec that no code names in an `@spec:` comment")


def _fixing(options: Options) -> list[str]:
    """The ledger rows a fix step's independent check answers (`verifier_brief`'s `rows`)."""
    held = options.ledger or {}
    return list(held.get("ids") or ()) if held.get("role") == "fix" else []


def _index_servers(plan: Plan, repo_dir: Path, workdir: Path) -> dict[str, tuple[Mapping[str, Any], str]]:
    """The code index the independent check is handed: an adopted neighbour whose index is present
    and that runs a read-only server (livespec, behind its guard), with the hint for reviewing.
    What a check reports it measures, and it may not change what it reads."""
    out: dict[str, tuple[Mapping[str, Any], str]] = {}
    for adapter, _ in plan.active:
        server = getattr(adapter, "read_only_server", lambda: None)()
        if server is not None:
            kind = "review-critical" if plan.risk.tier == "high" else "review-routine"
            out[adapter.name] = (server, f"{adapter.hint(kind, repo_dir, workdir)} Read-only: a call that would "
                                         "change the index is refused.")
    return out


def _ahead(result: launch.WorkerResult, *, options: Options, verifying: bool,
           workspace: isolate.Workspace | None, cell: Cell, plan: Plan, text: str, spec: launch.LaunchSpec,
           workdir: Path, base: str | None, ledger: dict[str, Mapping[str, str]], launcher: Launcher,
           registry: Mapping[str, caps.Capability] | None, store: Store | None = None,
           task: Mapping[str, Any] | None = None, corrections: int = 0,
           since: tuple[str, Sequence[str]] | None = None) -> Ahead | None:
    """The independent check, started now beside locks' check when it will be needed anyway: a
    passing writing attempt in a worktree, a check to run, a risk that asks for it. Never with
    services (two runs of their tests would share their ports) or with the `overlap` setting off."""
    if not (verifying and workspace is not None and options.verify and not options.services
            and config.enabled("overlap", os.environ)):
        return None
    so_far = list(isolate.changed(workspace))
    _risk_of_change(plan, so_far, workdir, base)
    if not so_far or not risk_mod.needs_verifier(plan.risk, cheap=_cheap(cell)):
        return None
    if store is not None and task is not None and not _sampling(store, task, plan, cell, options, corrections,
                                                                ledger).check:
        return None  # its cell's record let this pass through unchecked: nothing to run beside the check
    read_at = isolate.snapshot(workdir) or ""
    check, _ = _check_spec(text=text, kind=plan.kind, cell=cell, spec=spec, workdir=workdir, base=base,
                           changed=so_far, risk=plan.risk, ledger=ledger, registry=registry,
                           rules=[n for n in plan.notes if n.get("kind") == notes.CONSTRAINT],
                           rows=_fixing(options), running=options.verify,
                           impact=_impact(plan, so_far, workspace.repo_dir, workdir=workdir, base=base),
                           index=_index_servers(plan, workspace.repo_dir, workdir), since=since)
    return Ahead(launcher, check, so_far, options.verify, read_at=read_at, since=since)


class Ahead:
    """The independent check started beside locks' own (the `overlap` setting): it reads the diff the
    check runs on, so neither waits for the other. A check that fails stops it and its verdict is
    dropped, as one that never ran; a check that passes waits only for what is left of it."""

    def __init__(self, launcher: Launcher, check: launch.LaunchSpec, changed: Sequence[str], running: str, *,
                 read_at: str = "", since: tuple[str, Sequence[str]] | None = None) -> None:
        self.changed, self.running = tuple(changed), running
        #: The snapshot of the worktree it reads (`isolate.snapshot`), and the round it re-checks from.
        self.read_at, self.since = read_at, since
        #: Files locks' check itself wrote while it ran (a formatter's): no change of the worker's.
        self.tolerated: tuple[str, ...] = ()
        self.runner = launch.Stoppable()
        self.began = time.monotonic()
        self.ended: float | None = None
        self._found: launch.WorkerResult | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(launcher, check), daemon=True)
        self._thread.start()

    def _run(self, launcher: Launcher, check: launch.LaunchSpec) -> None:
        try:
            self._found = _launch(launcher, check, self.runner)
        except BaseException as exc:  # re-raised where the verdict is read, as a launch's own error is
            self._error = exc
        self.ended = time.monotonic()

    @property
    def beside_s(self) -> float:
        """How long it ran while locks' check did, up to now."""
        return (self.ended or time.monotonic()) - self.began

    def result(self) -> launch.WorkerResult:
        self._thread.join()
        if self._error is not None:
            raise self._error
        assert self._found is not None
        return self._found

    def stop(self) -> launch.WorkerResult | None:
        """Stop it (its process group) and wait for it: what it had cost is still kept."""
        self.runner.stop()
        self._thread.join()
        return self._found


def _sampling(store: Store, task: Mapping[str, Any], the_plan: Plan, cell: Cell, options: Options, corrections: int,
              ledger: Mapping[str, Mapping[str, str]]) -> trust.Sampling:
    """Whether the independent check a pass is due reads it (`locks.trust`). Only a medium change a cheap
    cell made is ever sampled, and only as its task's first inspection: a high risk, a correction's
    re-check, a resume that carries blockers (`ledger`) and a fix of ledger rows are read every time."""
    if (the_plan.risk.tier != "medium" or not _cheap(cell) or corrections or ledger or _fixing(options)
            or options.ledger):
        return trust.Sampling(True)
    return trust.sampling(store, task.get("repo"), the_plan.kind, cell, int(task["id"]), tier=the_plan.risk.tier)


def _read_before(store: Store, task_id: int, cell: Cell, corrections: int,
                 workdir: Path | None) -> tuple[str, list[str]] | None:
    """For a correction's re-check: the snapshot the round before read and the files changed since it,
    when it read this worktree on this model (a new model starts from the base, and its change is all
    new); None otherwise, and the re-check reads the whole change."""
    if corrections <= 0 or workdir is None:
        return None
    last = store.last_event(task_id, "verified")
    data = (last or {}).get("data") or {}
    if not data.get("read_at") or data.get("late") or not data.get("worker"):
        return None
    try:
        if Cell.parse(str(data["worker"])).model != cell.model:
            return None
    except ValueError:
        return None
    files = isolate.changed_since(workdir, str(data["read_at"]))
    return (str(data["read_at"]), files) if files is not None else None


def recheck_lines(sha: str, files: Sequence[str]) -> str:
    """What a correction's re-check is told of the change the round before read: what it changed since."""
    short = sha[:12]
    if not files:
        return (f"This is a correction's re-check. The previous check read the whole change at the snapshot commit "
                f"{short}, and nothing changed since: say for each problem it found whether it is fixed, from what "
                "is there.")
    shown = ", ".join(files[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
    return (f"This is a correction's re-check. The previous check read the whole change at the snapshot commit "
            f"{short}; since then the correction changed {len(files)} file(s): {shown}. Read those first "
            f"(`git diff {short} -- <file>`; a file it added is new, read it whole), say for each problem found "
            "before whether it is fixed, and probe what the correction's changes reach. The rest of the change "
            "was read by that check: report a new problem there only when the correction reaches it or a probe "
            "shows it.")


def reask_lines(missing: Sequence[str], correct: Mapping[str, str]) -> str:
    """What an independent check that left CORRECT lines unanswered is asked again: those, each."""
    return ("Your answer left these CORRECT lines unanswered. Answer each one in `criteria` by its id, whether the "
            "change meets it and what shows it; your earlier findings stand:\n"
            + "\n".join(f"{cid}: {correct[cid]}" for cid in missing))


def _merged(first: launch.WorkerResult, again: launch.WorkerResult) -> launch.WorkerResult:
    """A check asked again for what it left unanswered: its criteria answered either time (the later
    answer wins), every finding of both, both costs; passed as the later answer says."""
    said = {c["id"]: c for c in first.criteria}
    said.update({c["id"]: c for c in again.criteria})
    found = list(first.findings)
    found += [f for f in again.findings if f not in found]
    return dataclasses.replace(again, criteria=tuple(said.values()), findings=tuple(found),
                               cost_usd=first.cost_usd + again.cost_usd, turns=first.turns + again.turns,
                               duration_s=first.duration_s + again.duration_s)


def _correct_lines(verdict: launch.WorkerResult, correct: Mapping[str, str]) -> tuple[list[dict[str, str]], list[str]]:
    """The CORRECT lines (`C#`, `tracer.criteria`) a check said the change does not meet, as blockers,
    and the ones it left unanswered."""
    said = {c["id"]: c for c in verdict.criteria}
    blockers = [{"where": cid, "what": f"{cid} is not met ({line}): {said[cid]['evidence'] or 'no evidence given'}",
                 "evidence_class": "deterministic" if said[cid]["evidence"] else "inferential",
                 "cause": "introduced", "proof": said[cid]["evidence"]}
                for cid, line in correct.items() if cid in said and not said[cid]["met"]]
    return blockers, [cid for cid in correct if cid not in said]


def _independent_check(result: launch.WorkerResult, *, text: str, kind: str, cell: Cell, spec: launch.LaunchSpec,
                       workdir: Path, base: str | None, changed: Sequence[str], risk: risk_mod.Risk,
                       ledger: dict[str, Mapping[str, str]], corrections: int, launcher: Launcher, store: Store,
                       task_id: int, nth: int, say: Callable[[str], None], outgrew: bool = False,
                       registry: Mapping[str, caps.Capability] | None = None,
                       rules: Sequence[Mapping[str, Any]] = (), rows: Sequence[str] = (),
                       ahead: Ahead | None = None, impact: Sequence[str] = (),
                       index: Mapping[str, tuple[Mapping[str, Any], str]] | None = None,
                       snapshots: bool = False, sampled: str = ""
                       ) -> tuple[launch.WorkerResult, stops.Stop | None, dict[str, Mapping[str, str]], int,
                                  tuple[dict[str, str], ...]]:
    """A pass checked again by a worker that verifies the request, not the work.

    The first problems it finds earn one correction. A later round is earned by
    a problem that survived (until `MAX_CORRECTIONS`), or by progress: every
    problem of the round before fixed, and a new one a probe shows. A new problem
    found after that progress and only read from the code does not stop the work:
    the pass stands and carries it, for a person to read. Whatever is left at the
    cap stops the task for a person, its work kept. A check that could not decide
    leaves the pass standing, and says so. Work whose worker ran out of turns
    before its own check (`outgrew`) failed for room, not for a bug: the next
    attempt gets more turns on the same cell, never a stronger model.

    A correction's re-check in a worktree (`snapshots`) reads first what the correction changed since
    the snapshot the round before read (`_read_before`), the rest of the change being read already."""
    since = _read_before(store, task_id, cell, corrections, workdir if snapshots else None)
    check, vcell = _check_spec(text=text, kind=kind, cell=cell, spec=spec, workdir=workdir, base=base,
                               changed=changed, risk=risk, ledger=ledger, registry=registry, rules=rules, rows=rows,
                               running=ahead.running if ahead is not None else "", impact=impact, index=index,
                               since=since)
    say(f"locks: #{task_id} attempt {nth} passed; risk {risk.tier}: an independent check at {vcell.label}"
        + (" (it ran beside locks' check)" if ahead is not None else ""))
    mark = time.monotonic()
    beside = ahead is not None and set(changed) <= {*ahead.changed, *ahead.tolerated} and ahead.since == since
    if beside:
        assert ahead is not None
        verdict, overlapped_s = ahead.result(), ahead.beside_s
        mark, read_at = ahead.began, ahead.read_at
    else:
        if ahead is not None:
            ahead.stop()  # the check changed what it read: read the change as it stands now
        read_at = (isolate.snapshot(workdir) or "") if snapshots else ""
        verdict, overlapped_s = launcher(check), 0.0
    correct = dict(tracer_mod.criteria(workdir, text)[0]) if config.enabled("tracer", os.environ) else {}
    reasked: list[str] = []
    if correct and (missing := _correct_lines(verdict, correct)[1]):
        # It left CORRECT lines unanswered: asked once more for those alone, in a session of its own.
        reasked = missing
        again = launcher(dataclasses.replace(check, prompt=check.prompt + "\n\n" + reask_lines(missing, correct)))
        verdict = _merged(verdict, again)
    wall_s = time.monotonic() - mark
    fixing = tuple(rows)
    return _judged(result, verdict, vcell=vcell, wall_s=wall_s, overlapped_s=overlapped_s, beside=beside, risk=risk,
                   ledger=ledger, corrections=corrections, store=store, task_id=task_id, nth=nth, say=say,
                   outgrew=outgrew, rows=fixing, correct=correct,
                   recorded={**({"tracer_reasked": reasked} if reasked else {}),
                       "worker": cell.label, **({"read_at": read_at} if read_at else {}),
                       **({"delta": {"since": since[0], "files": list(since[1])[:50], "n": len(since[1])}}
                          if since is not None else {}),
                       **({"sampled": sampled} if sampled else {})})


def _check_spec(*, text: str, kind: str, cell: Cell, spec: launch.LaunchSpec, workdir: Path, base: str | None,
                changed: Sequence[str], risk: risk_mod.Risk, ledger: Mapping[str, Mapping[str, str]],
                registry: Mapping[str, caps.Capability] | None, rules: Sequence[Mapping[str, Any]],
                rows: Sequence[str], running: str = "", impact: Sequence[str] = (),
                index: Mapping[str, tuple[Mapping[str, Any], str]] | None = None,
                since: tuple[str, Sequence[str]] | None = None) -> tuple[launch.LaunchSpec, Cell]:
    """The independent check's launch: its cell, its brief and the capabilities that measure, and
    the code index (`index`: name -> read-only server and its hint) where the repository has one."""
    role = roles.internal("verifier")
    vcell = verify_cell(cell)
    # It keeps only the capabilities that measure, the work's and any the change touches: what it reports,
    # it measures.
    measures = caps.measuring(registry or {})
    measured_by = plugins.measure_hints()
    hints = {name: measured_by.get(name) or measures[name].hint
             for name in [*spec.mcp_servers, *measures] if name in measured_by or name in measures}
    measuring = {name: server for name, server in spec.mcp_servers.items() if name in hints}
    touched = caps.select({n: c for n, c in measures.items() if n not in measuring}, kind, workdir=workdir,
                          paths=changed, reading=True)
    measuring.update(touched.servers)
    hints = {name: hints[name] for name in measuring}
    for name, (server, hint) in (index or {}).items():
        measuring[name], hints[name] = server, hint
    fixing = tuple(rows)
    # The CORRECT lines of the flows the request names, each a criterion the check answers (`tracer.criteria`).
    lines = tracer_mod.criteria(workdir, text)[0] if config.enabled("tracer", os.environ) else []
    check = dataclasses.replace(
        spec, criteria=(*spec.criteria, *(cid for cid, _ in lines)),
        prompt=verifier_brief(text, kind, workdir, base, changed, list(ledger.values()), risk.reasons,
                                    rules=rules, rows=fixing, running=running, impact=impact,
                                    method=tracer_mod.verifier_lines(workdir, text, tuple(measuring))
                                    if config.enabled("tracer", os.environ) else (), since=since),
        verdicts=fixing, verdict_words=ledger_mod.WORDS["recheck"] if fixing else (),
        cell=vcell, max_turns=min(spec.max_turns, VERIFIER_TURNS),
        # Its calls are never the worker's: the log keeps them under no attempt, so what a worker used
        # (`evidence.owed`, `capability_calls`, `behavior`) is read from its own calls alone.
        env=_verifier_env(spec.env),
        # Its own session, never the worker's: it reads the request and the diff, not the work's account.
        resume_session="", fork_session=False, resume_note="", persist_session=False,
        timeout_s=launch.timeout_for(min(spec.max_turns, VERIFIER_TURNS)),
        model_id=models.pinned(vcell.model), permission_mode=grants.mode_for(vcell.model),
        disallowed_tools=tuple(dict.fromkeys([*spec.disallowed_tools, *launch.WRITE_TOOLS])),
        append_system_prompt="\n\n".join([roles.brief(role).system_prompt,
                                           *(f"{name}: {hint}" for name, hint in hints.items() if hint)]),
        mcp_servers=measuring,
        allowed_tools=tuple(r for r in spec.allowed_tools
                            if not r.startswith("mcp__") or r.removeprefix("mcp__") in measuring)
        + tuple(f"mcp__{name}" for name in [*touched.servers, *(index or {})]))
    return check, vcell


def _read_only(path: Path, read_from: str, store: Store, task_id: int, nth: int,
               say: Callable[[str], None]) -> list[str]:
    """What the independent check wrote in the worktree it read, put back as it was before it ran:
    its write tools are taken away, but a shell writes too, and what it wrote would be committed as
    the work. Said and kept as a `verifier_wrote` event; never a verdict. Never fails a run."""
    try:
        wrote = isolate.changed_since(path, read_from) or []
        if not wrote:
            return []
        left = isolate.put_back(path, read_from, wrote)
    except Exception:  # a guard never costs the run it guards
        return []
    store.add_event(task_id, "verifier_wrote", seq=nth, files=wrote[:200], n=len(wrote), left=left[:50])
    say(f"locks: #{task_id} the independent check wrote {len(wrote)} file(s) in the work it read; put back"
        + (f", {len(left)} could not be" if left else ""))
    return wrote


def _verifier_env(env: Mapping[str, str]) -> dict[str, str]:
    """A worker's environment for the independent check of its attempt: the same task (it starts no
    locks work), its attempt marked as the check's, which the tool-call log keeps as no attempt."""
    out = dict(env)
    if out.get("LOCKS_WORKER_ATTEMPT"):
        out["LOCKS_WORKER_ATTEMPT"] = out["LOCKS_WORKER_ATTEMPT"] + "-verify"
    return out


def _judged(result: launch.WorkerResult, verdict: launch.WorkerResult, *, vcell: Cell, wall_s: float,
            overlapped_s: float, beside: bool, risk: risk_mod.Risk, ledger: dict[str, Mapping[str, str]],
            corrections: int, store: Store, task_id: int, nth: int, say: Callable[[str], None], outgrew: bool,
            rows: Sequence[str], recorded: Mapping[str, Any] | None = None, correct: Mapping[str, str] | None = None
            ) -> tuple[launch.WorkerResult, stops.Stop | None, dict[str, Mapping[str, str]], int,
                       tuple[dict[str, str], ...]]:
    """What the independent check's verdict does to the pass: a correction, a stop, or nothing."""
    cost = result.cost_usd + verdict.cost_usd
    shown = findings_mod.read({"findings": list(verdict.findings)})
    blockers = findings_mod.blocking(shown)
    # A CORRECT line of a flow the request names that the check says the change does not meet blocks it,
    # as a finding does; one it left unanswered is said, never taken as met.
    unmet_lines, unanswered = _correct_lines(verdict, correct or {})
    blockers += [b for b in unmet_lines if not any(findings_mod.same(b, f) for f in blockers)]
    # A row of the fix it says is still open is a problem of this change, as a finding is.
    answered = {v["id"]: v for v in verdict.verdicts if v["id"] in rows}
    blockers += [{"where": v["id"], "what": f"{v['id']} is {v['verdict'].replace('_', ' ')}: {v['proof']}",
                  "evidence_class": "deterministic" if v["proof"] else "inferential", "cause": "introduced",
                  "proof": v["proof"]}
                 for v in answered.values() if v["verdict"] != "fixed"
                 and not any(findings_mod.same({"where": v["id"], "what": v["proof"]}, b) for b in blockers)]
    if not verdict.passed and not blockers and verdict.verdict == "fail":
        blockers = [{"where": "", "what": verdict.summary or "the check failed without a finding",
                     "evidence_class": "inferential", "cause": "unknown", "proof": ""}]
    decided = verdict.passed or bool(blockers)
    still = [f for f in blockers if any(findings_mod.same(f, g) for g in ledger.values())]
    # Progress: the round before had problems and none of them is left.
    progress = corrections > 0 and not still
    carried = [f for f in blockers if f["evidence_class"] != "deterministic"] if progress else []
    if carried:
        blockers = [f for f in blockers if f not in carried]
    store.add_event(task_id, "verified", seq=nth, cell=vcell.label, risk=risk.data(), verdict=verdict.verdict,
                    passed=not blockers and decided, findings=list(shown)[:20], blocking=len(blockers),
                    blockers=list(blockers)[:20],
                    carried=len(carried), cost_usd=verdict.cost_usd, summary=verdict.summary[:500],
                    round=corrections, wall_s=round(wall_s, 2), duration_s=round(verdict.duration_s, 2),
                    turns=verdict.turns, **({"verdicts": list(answered.values())} if rows else {}), **(recorded or {}),
                    **({"tracer_unanswered": unanswered} if unanswered else {}),
                    **({"beside_check": True, "beside_check_s": round(overlapped_s, 2)} if beside else {}))
    if not decided:
        say(f"locks: #{task_id} the independent check could not decide; the pass stands")
        return dataclasses.replace(result, cost_usd=cost, evidence=result.evidence + "\n\n[independent check "
                                   f"inconclusive: {clip(verdict.summary, 300)}]"), None, ledger, corrections, ()
    if not blockers:
        if carried:
            say(f"locks: #{task_id} the earlier problems are fixed; {len(carried)} new one(s) read from the code "
                "go with the pass")
            return dataclasses.replace(
                result, cost_usd=cost, findings=(*result.findings, *carried),
                evidence=result.evidence + "\n\n[independent check: the problems it found before are fixed. "
                f"New, read from the code and not shown by a probe, for a person to read:\n"
                f"{findings_mod.text(carried)[:1500]}]"), None, ledger, corrections, ()
        return dataclasses.replace(result, cost_usd=cost), None, ledger, corrections, ()
    found = findings_mod.text(blockers)
    failure = Failure.TURNS_EXHAUSTED if outgrew else Failure.CODE_BUG
    room = " (the worker had run out of turns before checking its own work)" if outgrew else ""
    if corrections == 0 or ((still or progress) and corrections < MAX_CORRECTIONS):
        if corrections == 0 or progress:
            ledger = {findings_mod.key(f): f for f in blockers}
        say(f"locks: #{task_id} the independent check found {len(blockers)} problem(s): a correction")
        failed = dataclasses.replace(
            result, passed=False, failure=failure, cost_usd=cost,
            summary=f"the independent check found {len(blockers)} problem(s) this change caused{room}: "
                    f"{result.summary}",
            evidence=f"The independent check found:\n{found}\n\nIts probes:\n{verdict.evidence[-1500:]}")
        return failed, None, ledger, corrections + 1, tuple(blockers)
    why = (f"after {MAX_CORRECTIONS} corrections it still shows problems the earlier checks had not"
           if progress else f"the same problem survived {MAX_CORRECTIONS} corrections" if len(still) == len(blockers)
           else f"after {MAX_CORRECTIONS} corrections a problem it found before is still there, beside new ones")
    stop = stops.Stop("review", f"the independent check still finds problems; {why}", attempt=nth,
                      account=clip(found, 2000))
    failed = dataclasses.replace(result, passed=False, failure=failure, cost_usd=cost,
                                 summary=f"the independent check still finds problems{room}: {result.summary}",
                                 evidence=found)
    return failed, stop, ledger, corrections, tuple(blockers)


#: Reading kinds whose task names a change by nature: the change is for the work after them.
PLANNING_KINDS = frozenset({"plan"})
#: What a planning worker is told of the change its task names, in place of the `spec_bug` line:
#: a real plan step of a workflow ("Plan … : Add a function …") answered `spec_bug`, read-only.
PLAN_CHANGE = ("the change the task names is for the work that follows you: planning it is the "
               "deliverable, never a `spec_bug`, and you make none of it here.")


def brief(
    text: str,
    kind: str,
    workdir: Path,
    attempts: Sequence[Attempt],
    dead_ends: Sequence[dict],
    verify: str | None,
    writes: bool,
    context: Sequence[str] = (),
    surfaces: Sequence[str] = (),
) -> str:
    parts = [f"Task ({kind}):\n{text}", f"Work in: {workdir}"]
    if listed := criteria_mod.brief(text):
        parts.append(listed)
    if bounds := surfaces_mod.brief(surfaces):
        parts.append(bounds)
    if context:
        parts.append("\n".join(context))
    if not writes:
        parts.append("This task is read-only: find out and report. Do not change files. The answer is the "
                     "deliverable, whatever it says: when you checked what was asked, the verdict is `pass`, also "
                     "when what you found is broken, missing or failing. Put the answer in `summary`, each finding "
                     "in `findings` (where, what, how it is known, and its proof), and what shows them (file:line, "
                     "a command and its output) in `evidence`. `code_bug` is never about the "
                     "code you read: use `inconclusive` for something you could not check, and "
                     + (PLAN_CHANGE if kind in PLANNING_KINDS else
                        "`fail` with `spec_bug` when the task asks for a change, which you cannot make here."))
    if verify:
        parts.append(f"When you report a pass, it is checked by running: {verify}")
        if writes and (near := ci.related(verify, workdir)):
            files = "<the test files of what you changed>" if near[0] == "pytest" else "<the files you changed>"
            parts.append(f"While you work, run only the tests near your change ({near[1].format(files=files)}): "
                         f"the full `{verify}` is slow, and locks runs it when you are done. Do not run it yourself "
                         "before you report: locks runs it as soon as you do, and hands you its failures to fix.")
    if dead_ends:
        lines = ["Fixes already tried against similar problems, here or in another repository. "
                 "Do not repeat one without saying why it would work this time:"]
        for d in dead_ends:
            where = f"{d['repo']}, {_SCOPE_SAID[d['scope']]}" if d.get("scope") in _SCOPE_SAID else d["repo"]
            line = f"- ({where}) {d['problem']}: tried {clip(d['tried'], 200, one_line=True)}"
            if d["why"]:
                line += f"; failed because {clip(d['why'], 200, one_line=True)}"
            if d["worked_instead"]:
                line += f"; what worked: {clip(d['worked_instead'], 200, one_line=True)}"
            lines.append(line)
        parts.append("\n".join(lines))
    if attempts:
        lines = ["Previous attempts on this task — already tried, do not repeat:"]
        for i, a in enumerate(attempts, 1):
            lines.append(f"  attempt {i} [{a.cell.label}, {a.failure or 'no verdict'}] {clip(a.summary, 300)}")
            if a.denied:
                lines.append(f"    refused: {', '.join(a.denied[:5])}")
        if failed := failed_output(attempts[-1]):
            lines.append("  " + failed)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _reshape_prompt(attempts: Sequence[Attempt]) -> str:
    """What a worker asked again on its own cell is told: write the spec its build owes, or answer
    in the form it was given."""
    if attempts and attempts[-1].shape.startswith(evidence_mod.SHAPE):
        return evidence_mod.PROMPT
    return specs_mod.FIRST_PROMPT if attempts and attempts[-1].shape.startswith(specs_mod.FIRST_SHAPE) \
        else launch.RESHAPE_PROMPT


def continuation_note(last: Attempt) -> str:
    """What a continued session is told failed its attempt, which its context does not
    hold: the independent check's blockers in a correction round (its work passed its
    own check), else the output that failed it."""
    if last.shape.startswith((specs_mod.FIRST_SHAPE, evidence_mod.SHAPE)):
        return f"Why your last answer was held back: {last.shape}."
    if last.shape:
        return f"Why your last answer could not be read: {last.shape}."
    if last.blockers:
        return ("The independent check, which reads only the request and the diff, found these problems in your "
                "work after it passed its own check:\n" + findings_mod.text(last.blockers)[:3000])
    return failed_output(last)


def failed_output(last: Attempt) -> str:
    """The output that failed an attempt, for the next one: a fresh brief and a session
    that continues alike, since a red check is evidence for whatever comes next."""
    if last.passed:
        return ""
    if last.check_tail.strip():
        return ("What locks' own check said when it failed the last attempt (the failing tests, then the last "
                "lines of its output): start from this, do not re-run the whole check to find it out:\n"
                + last.check_tail.strip())
    if not last.evidence.strip():
        return ""
    return "The output that failed the last attempt, last lines:\n" + last.evidence.strip()[-1500:]


def _equip(selection: caps.Selection, the_plan: Plan, repo_dir: Path, workdir: Path,
           withheld: frozenset[str] = frozenset()) -> caps.Selection:
    """The registry's selection plus every adopted neighbour, each with its hint, less the ones a
    trial withholds from this worker (`locks.handing`). A registry entry of the same name wins: a
    person who configured it chose it."""
    servers = dict(selection.servers)
    hints = list(selection.hints)
    names = list(selection.names)
    for adapter, status in the_plan.active:
        if adapter.name in withheld:
            continue
        # A neighbour that can hold its worker to the tools its brief names hands only those.
        worker_server = getattr(adapter, "worker_server", None)
        server = worker_server(the_plan.kind, status) if worker_server else adapter.server()
        if server is None or adapter.name in servers:
            continue
        servers[adapter.name] = server
        hints.append(f"{adapter.name}: {adapter.hint(the_plan.kind, repo_dir, workdir)}")
        names.append(adapter.name)
    return caps.Selection(servers, tuple(hints), tuple(names), selection.read_dirs)


#: The run of a check at a base gets what the work's own run of it took times this, plus the margin:
#: the base shares the machine with whatever else runs, and a check that fits its limit in the work
#: by minutes may not at the base, where a timeout says nothing of what failed there.
BASE_TIMEOUT_FACTOR, BASE_TIMEOUT_MARGIN_S = 2, 300


def base_timeout_s(took_s: float) -> float:
    """The time a check's run at the base is given, from how long the work's own run took (0 when
    none was seen): never under the limit the work's own run has, and above what it took."""
    return max(VERIFY_TIMEOUT_S, took_s * BASE_TIMEOUT_FACTOR + BASE_TIMEOUT_MARGIN_S)


def _preflight(command: str, workdir: Path, task: Mapping[str, Any], store: Store, say: Callable[[str], None],
               *, installing: bool = False) -> preflight.Result:
    """`locks.preflight` on the task's check, before any attempt of this run: what it could not tell and
    what it warns of are said, what stops it too, and a `preflight` event keeps all of it."""
    flight = preflight.check(command, workdir, asked=f"{task.get('title') or ''}\n{task.get('body') or ''}",
                             installing=installing)
    for line in flight.plan_lines():
        say(f"locks: #{task['id']} {line}")
    store.add_event(task["id"], "preflight", ok=flight.ok, blockers=[b.line() for b in flight.blockers],
                    unknown=flight.unknown, warnings=flight.warnings)
    if not flight.ok:
        say(f"locks: #{task['id']} {flight.reason()}")
    return flight


def _run_check(command: str, workdir: Path, timeout: float | None = None) -> tuple[int | None, str]:
    """The repository's own check: (exit code, or None on a timeout; its whole output). It has
    `VERIFY_TIMEOUT_S` unless `timeout` says otherwise (the base's run: `base_timeout_s`)."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workdir), capture_output=True, text=True,  # noqa: S602
                              timeout=VERIFY_TIMEOUT_S if timeout is None else timeout, check=False,
                              env={**os.environ, **runtime.env_for(workdir)})
    except subprocess.TimeoutExpired:
        return None, ""
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _timed_check(command: str, workdir: Path, baseline: Baseline | None) -> tuple[int | None, str]:
    """`_run_check`, its duration noted on the baseline: the run at the base is sized from it."""
    mark = time.monotonic()
    ran = _run_check(command, workdir)
    if baseline is not None:
        baseline.took(time.monotonic() - mark)
    return ran


#: How much of a check's output an attempt's evidence keeps.
CHECK_OUTPUT_KEPT = 3000


#: Where a check's run at a base commit is kept (`Store.meta`), and for how long: a task's
#: later attempts, its verifier rounds and its sibling parts on the same base read it there.
BASE_RUN_META, BASE_RUN_KEPT_S = "check_base:", 24 * 3600
#: Where the last base run of a command in a repository that could tell what failed there is kept
#: (`Store.meta`), at whatever commit: what a base that cannot tell is read against. It does not expire.
KNOWN_BASE_META = "check_base_last:"
#: How many failing lines a `check_preexisting` event and an attempt's evidence name.
FAILURES_SHOWN = 10


@dataclass
class Baseline:
    """What a red check is compared with: the same command at the commit the task's work
    started from (`isolate.at_base`). `must_fix` is work whose job is to make a failure go
    away (`_REPRODUCES`): the same failures, untouched, are not its pass."""
    store: Store
    task_id: int
    repo_dir: Path
    base: str
    root: Path
    must_fix: bool = False
    #: How many times the check runs (`flake_fix_runs`): more than one for work that fixes a flake.
    runs: int = 1
    #: Whether the last check's runs disagreed: work that fixes a flake then fails, never excused by the base.
    flaked: bool = False
    #: The last check's account when its failures were all there at the base, else None.
    found: dict[str, Any] | None = None
    #: How long the work's own run of the check took, the longest seen: the base's run is sized from it.
    check_s: float = 0.0
    #: Why the last check's failure could not be told from the base's, else "" (`_against_base`).
    unknown: str = ""
    #: A run that started in a folder that is no checkout: each repository inside it and its commit
    #: (`base` is then their line, `isolate.places_label`). Empty for a run in one checkout.
    places: tuple[tuple[str, str], ...] = ()
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _owner: int = field(default_factory=threading.get_ident, repr=False)

    @contextlib.contextmanager
    def _db(self):
        """The store, or one of its own on another thread (a race checks each try in its own)."""
        if threading.get_ident() == self._owner:
            yield self.store
            return
        other = Store(self.store.path)
        try:
            yield other
        finally:
            other.close()

    def took(self, seconds: float) -> None:
        """Note how long a run of the work's own check took: the longest is kept."""
        self.check_s = max(self.check_s, seconds)

    def _known_key(self, command: str) -> str:
        return KNOWN_BASE_META + hashlib.sha256(f"{self.repo_dir}\0{command}".encode()).hexdigest()[:32]

    def known(self, command: str) -> dict[str, Any] | None:
        """The last base run of the command in this repository that could tell what failed there, at
        whatever commit: {"at", "base", "failures"}, or None when none ever could."""
        with self._db() as db:
            kept = db.meta(self._known_key(command))
        try:
            found = json.loads(kept) if kept else None
        except ValueError:
            return None
        return found if isinstance(found, dict) and found.get("base") else None

    @contextlib.contextmanager
    def _base_tree(self):
        """(the base's copy, where the check runs in it): one detached worktree, or a folder of them
        for a run that started in a folder of repositories. The check runs where the work's did: a
        run sent from a folder inside a checkout runs it in that folder's copy, not the root's."""
        if self.places:
            with isolate.at_bases(self.repo_dir, self.places, self.root) as path:
                yield path, path
            return
        with isolate.at_base(self.repo_dir, self.base, self.root) as path:
            top = repo.toplevel(self.repo_dir)
            inside = self.repo_dir.resolve().relative_to(top.resolve()) if top is not None else Path()
            yield path, path / inside if (path / inside).is_dir() else path

    def at_base(self, command: str) -> dict[str, Any]:
        """The command's run at the base: {"code", "failures", "cannot"}, from the store's
        meta when it ran there in the last `BASE_RUN_KEPT_S`, else run now and kept. A run
        that timed out, could not run, lost the network or timed its tests out is `cannot`:
        it cannot tell what was there before. Its time is sized from the work's own run
        (`base_timeout_s`), and the last run that could tell is kept apart (`known`)."""
        digest = hashlib.sha256(f"{self.repo_dir}\0{self.base}\0{command}".encode()).hexdigest()[:32]
        key = BASE_RUN_META + digest
        with self._lock:
            with self._db() as db:
                kept = db.meta(key)
            if kept is not None and not _expired(kept):
                return json.loads(kept)
            try:
                with self._base_tree() as (path, where):
                    code, output = _run_check(command, where, timeout=base_timeout_s(self.check_s))
                    failures = launch.check_failures(output, roots=(where, path))
            except (isolate.IsolationError, OSError) as exc:
                return {"code": None, "failures": [],
                        "cannot": clip(f"no worktree could be made at the base ({exc})", 300, one_line=True)}
            cannot = ("it timed out" if code is None else "" if code == 0 else
                      cannot_run(code, output) or launch.check_timed_out(output) or launch.check_offline(output))
            found = {"at": time.time(), "code": code, "failures": sorted(failures), "cannot": cannot}
            # A timeout, a loaded machine or a dropped network is a moment's: the next try runs it again
            # (a timeout kept would read as the base's answer to every task on the same commit for a day).
            moment = code is None or bool(code and (launch.check_timed_out(output) or launch.check_offline(output)))
            if not moment:
                with self._db() as db:
                    db.set_meta(key, json.dumps(found))
                    db.drop_meta([k for k, v in db.metas(BASE_RUN_META).items() if _expired(v)])
                    if not cannot:
                        db.set_meta(self._known_key(command), json.dumps(
                            {"at": found["at"], "base": self.base, "failures": found["failures"]}))
            return found


def _expired(kept: str) -> bool:
    """A base's run kept longer than `BASE_RUN_KEPT_S`, or that cannot be read."""
    try:
        return time.time() - float(json.loads(kept).get("at") or 0) >= BASE_RUN_KEPT_S
    except (ValueError, AttributeError, TypeError):
        return True


def _against_base(baseline: Baseline | None, command: str, output: str, workdir: Path,
                  claim: str = "") -> tuple[bool, str]:
    """Whether a check that ran and failed on the work failed the same way at the base
    (`launch.preexisting`), and what the next attempt reads of the comparison. Fails closed:
    when the base passes, or no failing line can be read from either output, the failure is the
    work's. A base that could not tell (`_unsure`) is neither: `baseline.unknown` says why. `claim`
    is the worker's own account, which that comparison reads the failing tests against."""
    if baseline is None:
        return False, ""
    baseline.found = None
    baseline.unknown = ""
    if baseline.flaked:
        return False, (f"this work fixes a flaky test, and its check gave different results over {baseline.runs} "
                       "runs: the flake is still there, whatever the base does")
    work = launch.check_failures(output, roots=(workdir, baseline.repo_dir))
    if not work:
        return False, ("locks could not read which tests or files failed from its output, so it cannot tell "
                       "whether they failed before this work: the failure is read as this work's")
    at = baseline.at_base(command)
    short = isolate.base_label(baseline.base)
    if at.get("cannot"):
        return _unsure(baseline, command, work, at, claim)
    if at.get("code") == 0:
        return False, f"at the base commit {short} the same check passes: every failure here is this work's"
    base = frozenset(at.get("failures") or ())
    new, old = sorted(work - base), sorted(work & base)

    def listed(lines: Sequence[str]) -> str:
        more = len(lines) - FAILURES_SHOWN
        return "\n".join(f"  - {clip(x, 200, one_line=True)}" for x in lines[:FAILURES_SHOWN]) + (
            f"\n  - and {more} more" if more > 0 else "")

    if launch.preexisting(work, base, must_fix=baseline.must_fix):
        baseline.found = {"command": command, "base": baseline.base, "n": len(work), "base_n": len(base),
                          "fixed": len(base - work), "added": 0,
                          "failures": [clip(x, 200, one_line=True) for x in sorted(work)[:FAILURES_SHOWN]]}
        with baseline._db() as db:
            db.add_event(baseline.task_id, "check_preexisting", **baseline.found)
        return True, ""
    if not new:
        # Only work that must make a failure go away gets here: the same failures stand.
        return False, (f"this task's job is to make a failure go away, and the same {len(work)} failure(s) fail "
                       f"at the base commit {short} as here: none of them is gone yet\n{listed(old)}")
    said = f"compared with the same check at the base commit {short}: {len(new)} failing line(s) are new with this " \
           f"work, focus on them:\n{listed(new)}"
    if old:
        said += f"\n{len(old)} already failed at the base, before this work (not this task's to fix):\n{listed(old)}"
    return False, said


def _unsure(baseline: Baseline, command: str, work: frozenset[str], at: Mapping[str, Any],
            claim: str) -> tuple[bool, str]:
    """The same check at the base could not tell what fails there (it timed out, or could not run).
    The work's failing tests are read against the last known base result of this check here
    (`Baseline.known`): when every one is in it they were there before, as a base run would have
    said, and the pass stands. Otherwise nothing separates the work's failures from the
    environment's, and they are not charged to the work: `baseline.unknown` says so, with the
    comparison (the last known result, and how many of them the worker's own account names), and
    the task stops as `base unknown`."""
    said = f"at the base commit {baseline.base[:12]} the same check could not tell ({at['cannot']})"
    known = baseline.known(command)
    before = frozenset(known.get("failures") or ()) if known else frozenset()
    if known and launch.preexisting(work, before, must_fix=baseline.must_fix):
        baseline.found = {"command": command, "base": known["base"], "known_at": known["base"], "n": len(work),
                          "base_n": len(before), "fixed": len(before - work), "added": 0,
                          "failures": [clip(x, 200, one_line=True) for x in sorted(work)[:FAILURES_SHOWN]]}
        with baseline._db() as db:
            db.add_event(baseline.task_id, "check_preexisting", **baseline.found)
        return True, ""
    new = sorted(work - before)
    if known is None:
        earlier = "no earlier run of it at a base is known here to compare with"
    elif new:
        earlier = f"{len(new)} of them are not in the last known base result at {known['base'][:12]} ({few(new)})"
    else:
        earlier = (f"all of them are in the last known base result at {known['base'][:12]}, but this task's job is "
                   "to make a failure go away")
    named = sum(1 for line in work if _named(line, claim))
    baseline.unknown = (f"locks' check failed {len(work)} test(s) ({few(sorted(work))}) and {said}; {earlier}; "
                        f"its own account names {named} of the {len(work)} failing test(s)")
    return False, baseline.unknown


def preexisting_line(found: Mapping[str, Any] | None) -> str:
    """The report's line for a check whose failures were all there before the work."""
    if not found:
        return ""
    fixed, n, command = int(found.get("fixed") or 0), int(found.get("n") or 0), found.get("command")
    failures = f"{n} failure{'' if n == 1 else 's'}"
    if known := found.get("known_at"):
        return (f"`{command}` fails with {failures}, all in the last known base result at {str(known)[:12]} (this "
                "task's base run could not tell): they were there before this work, not caused by it"
                + (f", and {fixed} more that failed then pass now" if fixed else ""))
    if not fixed:
        return (f"`{command}` fails at the base too, with the same {failures}: they were there before this work, "
                "not caused by it")
    return (f"`{command}` fails at the base too: its {failures} here were all there before this work, not caused "
            f"by it, and {fixed} more that failed at the base pass now")


#: What a check prints when it could not run at all — its script, its manifest
#: or the Node its tools need is missing. Its exit then says nothing about the
#: work, and a stronger model meets the same wall. npm's EBADENGINE *warning*
#: is not here: it prints on runs that go on and pass.
_CANNOT_RUN = re.compile(
    r"^npm (?:ERR!|error) Missing script: .*$"
    r"|^npm (?:ERR!|error) enoent Could not read package\.json.*$"
    r"|^.*\brequires (?:a minimum )?Node\.js version\b.*$"
    r"|^.*The engine \"node\" is incompatible with this module.*$"
    # A container runtime the check needs and the place it runs in does not have (issue #119).
    r"|^.*\bSail is not running\..*$"
    r"|^.*\bDocker or Podman is not running\..*$"
    r"|^.*Cannot connect to the Docker daemon.*$"
    r"|^.*\bservice \"[^\"]*\" is not running.*$"
    r"|^.*no configuration file provided: not found.*$",
    re.MULTILINE)


def cannot_run(code: int | None, output: str) -> str | None:
    """Why a check that exited non-zero could not run at all, or None when it ran
    and failed. Exit 127 is the shell's "command not found"."""
    if code is None or code == 0:
        return None
    found = _CANNOT_RUN.search(output)
    if found:
        return found.group(0).strip()[:300]
    if code == 127:
        return "exit 127: the shell did not find the command"
    return None


#: A red check with at most this many failing tests runs once more: one that passes then is flaky.
FLAKY_MAX = 3
#: Where a repository's flaky tests are remembered (`Store.meta`), each with how often it flaked.
FLAKY_META = "flaky:"


#: The most of a repository's known flaky tests a brief names.
FLAKY_BRIEFED = 5
#: How many times the check of work that fixes a flaky test runs: a flake that is fixed holds on each.
FLAKE_FIX_RUNS = 3
_FLAKE_FIX = re.compile(
    r"\b(flak(y|e|es|iness)|intermittent\w*|intermitente\w*|sporadic\w*|espor[aá]dic\w*|a veces falla\w*"
    r"|sometimes fails|fails sometimes|(fail\w*|falla\w*|pass\w*|pasa\w*) \d+ (of|de|out of) \d+"
    r"|\d+ (of|de|out of) \d+ (runs?|times|veces|corridas))\b", re.IGNORECASE)


def readmes(text: str, repo_dir: Path) -> str | None:
    """A warning when a request about a README is made in a repository that holds several (a
    fork's, a package's beside the top one's): which one it means is the session's to say, and
    the top one is the one its host shows. None when it holds one or the request names none."""
    if not re.search(r"\breadme\b", text, re.IGNORECASE):
        return None
    top = repo.toplevel(repo_dir) or repo_dir
    found = sorted({str(p.relative_to(top)) for pattern in ("README*", "*/README*")
                    for p in top.glob(pattern) if p.is_file() and not any(
                        part.startswith(".") or part == "node_modules" for part in p.relative_to(top).parts)})
    if len(found) < 2:
        return None
    return (f"this task mentions a README and this repository has {len(found)}: {', '.join(found[:6])}; the one at "
            "the top is the one its host shows. Make sure the task names the one it means")


def flake_fix_runs(kind: str, text: str) -> int:
    """How many times a task's check runs: `FLAKE_FIX_RUNS` for writing work whose request is
    about a flaky or intermittent failure, since one run that passes proves nothing of a flake;
    once otherwise."""
    if kind in READ_ONLY_KINDS or not _FLAKE_FIX.search(text or ""):
        return 1
    return FLAKE_FIX_RUNS


def flaky_key(repo_dir: Path) -> str:
    """Where a repository's flaky tests are kept: by its key, so every worktree and session
    of it reads the same ones."""
    return FLAKY_META + hashlib.sha256(str(repo.key(repo_dir) or repo_dir).encode()).hexdigest()[:24]


def known_flaky(store: Store, repo_dir: Path) -> list[str]:
    """The tests locks saw flake here (failed, then passed on the rerun), most often first."""
    try:
        seen = json.loads(store.meta(flaky_key(repo_dir)) or "{}")
    except (ValueError, sqlite3.Error):
        return []
    return [line for line, _ in sorted(seen.items(), key=lambda kv: -int(kv[1] or 0))][:FLAKY_BRIEFED]


def _steady_check(command: str, workdir: Path, baseline: Baseline | None) -> tuple[int | None, str]:
    """The check, run again once when it ran and failed on a few tests (`FLAKY_MAX`): a second
    run that passes means those tests flake, which says nothing of the work. Its pass stands,
    recorded as a `check_flaky` event with the tests and kept per repository; anything else is
    the first run's failure, unchanged. Work that fixes a flake (`Baseline.runs`) gets its runs
    instead (`_repeated_check`)."""
    if baseline is not None:
        baseline.flaked = False
    code, full = _timed_check(command, workdir, baseline)
    if baseline is not None and baseline.runs > 1 and (code == 0 or _ran_and_failed(code, full)):
        return _repeated_check(command, workdir, baseline, code, full)
    if not _ran_and_failed(code, full):
        return code, full
    failing = launch.check_failures(full, roots=(workdir,))
    if not failing or len(failing) > FLAKY_MAX:
        return code, full
    again, second = _timed_check(command, workdir, baseline)
    if again != 0:
        return code, full
    lines = sorted(clip(x, 200, one_line=True) for x in failing)
    if baseline is not None:
        with baseline._db() as db:
            db.add_event(baseline.task_id, "check_flaky", command=command, failures=lines)
            key = flaky_key(baseline.repo_dir)
            seen = json.loads(db.meta(key) or "{}")
            for line in lines:
                seen[line] = int(seen.get(line) or 0) + 1
            db.set_meta(key, json.dumps(dict(sorted(seen.items(), key=lambda kv: -kv[1])[:50])))
    return 0, second + "\n\n" + f"(locks: {len(lines)} test(s) failed on the first run and passed on the second: " \
        + "; ".join(lines) + ")"


def _repeated_check(command: str, workdir: Path, baseline: Baseline, code: int | None,
                    full: str) -> tuple[int | None, str]:
    """The check of work that fixes a flake, run `baseline.runs` times (the first given): it
    passes only when every run does. Every run failing the same tests is one steady failure,
    compared with the base as any is; runs that disagree are the flake still there
    (`Baseline.flaked`), never excused by the base. Kept as a `check_repeated` event."""
    results = [(code, full)]
    while len(results) < baseline.runs and results[-1][0] is not None:
        results.append(_timed_check(command, workdir, baseline))
    failed = [(c, out) for c, out in results if c != 0]
    shapes = {frozenset(launch.check_failures(out, roots=(workdir,))) for _, out in failed}
    steady = not failed or (len(failed) == len(results) and len(shapes) == 1)
    n, passed = len(results), len(results) - len(failed)
    with baseline._db() as db:
        db.add_event(baseline.task_id, "check_repeated", command=command, runs=n, passed=passed, steady=steady)
    if not failed:
        return 0, results[-1][1] + f"\n\n(locks: this work fixes a flaky test: `{command}` ran {n} times and " \
            "passed every time)"
    first = failed[0]
    if steady:
        return first
    baseline.flaked = True
    return (first[0] if first[0] is not None else 1), first[1] + (
        f"\n\n(locks: this work fixes a flaky test, so `{command}` ran {n} times: it passed {passed} of {n}. A "
        "flake that is fixed passes every run; this output is the first run that failed)")


def _verified(result: launch.WorkerResult, command: str, workdir: Path,
              baseline: Baseline | None = None) -> launch.WorkerResult:
    """A claimed pass checked by the repository's own command. The worker does not
    grade itself: a red check turns the pass into a code bug, with the check's
    output as the evidence the next attempt reads. A check that could not run at
    all is the environment's, never a pass and never a bug in the work. A red check
    whose every failing line also fails at the base (`baseline`) was there before the
    work: the pass stands, and that is recorded and said (`_against_base`)."""
    if baseline is not None:
        baseline.found = None
    code, full = _steady_check(command, workdir, baseline)
    output = full[-CHECK_OUTPUT_KEPT:]
    if code is None:
        # The whole check ran past its own time after the worker's own run passed: the machine's
        # load, as tests out of time are (`check_timed_out`), never an answer to climb from.
        timed = f"the whole check ran past {VERIFY_TIMEOUT_S}s"
        return dataclasses.replace(result, passed=False, failure=Failure.ENVIRONMENT, load=timed, starved="load",
                                   evidence=output or timed,
                                   summary=f"`{command}` timed out after the claimed pass ({timed}): "
                                           f"{result.summary}")
    if code == 0:
        return result
    tail = _tail_of(full, workdir, baseline)
    if (why := cannot_run(code, output)) is not None:
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=output, check_tail=tail,
            summary=f"claimed a pass, but `{command}` could not run here ({why}): {result.summary}",
        )
    if timed := launch.check_timed_out(output):
        # The worker's own run passed and most of what failed here are tests out of time:
        # a machine busier than its CPUs, not the work. The level halves on it (`starved`).
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=output, load=timed, starved="load",
            check_tail=tail,
            summary=f"claimed a pass, but `{command}` timed its tests out, as a loaded machine does ({timed}): "
                    f"{result.summary}",
        )
    if dropped := launch.check_offline(output):
        # The check could not reach the network (an install, a fetch): its exit says
        # nothing about the work, and the same check, waited for, may well pass.
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=output, network=dropped,
            check_tail=tail,
            summary=f"claimed a pass, but `{command}` lost the network ({dropped}): {result.summary}",
        )
    before, compared = _against_base(baseline, command, full, workdir, claim=f"{result.summary}\n{result.evidence}")
    if before:
        return result
    shown = f"{output}\n\n{compared}" if compared else output
    failing = launch.check_failures(full, roots=(workdir,))
    if baseline is not None and baseline.unknown:
        # The base could not say what fails there: nothing shows the work caused this, so it is no
        # `code_bug` and no climb (`escalate._unknown_base`).
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=shown, failing=failing,
            base_unknown=baseline.unknown, check_tail=_tail_of(full, workdir, baseline, compared),
            summary=f"claimed a pass, but `{command}` exited {code} and the base could not say whether its failures "
                    f"were there before: {result.summary}",
        )
    return dataclasses.replace(
        result, passed=False, failure=Failure.CODE_BUG, evidence=shown, failing=failing,
        check_tail=_tail_of(full, workdir, baseline, compared),
        summary=f"claimed a pass, but `{command}` exited {code}: {result.summary}",
    )


def _tail_of(full: str, workdir: Path, baseline: Baseline | None, compared: str = "") -> str:
    """What a failed check kept of its output (`launch.check_tail`), the paths of the work's
    directory and of the base's copy taken off."""
    roots = (workdir, baseline.repo_dir) if baseline is not None else (workdir,)
    return launch.check_tail(full, roots, compared)


#: Attempts that stopped before the worker could check its own work, through no
#: verdict of its own: the repository's check is run for them.
_UNCHECKED = frozenset({Failure.TURNS_EXHAUSTED})


def _settles(result: launch.WorkerResult, command: str | None) -> bool:
    """A worker that could not run the command that would show its work — refused
    it, or ran out of turns first — and did not call it a failure: on a
    task that has written something, the repository's own check can still
    decide. Its claim alone never would. Run by locks, the check costs the
    worker nothing, and a red one hands the next attempt the failing output."""
    if not command or result.passed:
        return False
    if result.failure is Failure.PERMISSION:
        return result.verdict in ("inconclusive", "pass")
    return result.failure in _UNCHECKED


def _ran_and_failed(code: int | None, output: str) -> bool:
    """A check that ran and failed on the work: not a timeout, not one that could not run,
    not a loaded machine's or a dropped network's. Only such a failure is compared with the base."""
    return bool(code) and cannot_run(code, output) is None and not launch.check_timed_out(output) \
        and not launch.check_offline(output)


def _settled(result: launch.WorkerResult, command: str, workdir: Path,
             baseline: Baseline | None = None) -> launch.WorkerResult:
    if baseline is not None:
        baseline.found = None
    code, full = _steady_check(command, workdir, baseline)
    output = full[-CHECK_OUTPUT_KEPT:]
    if result.failure is Failure.PERMISSION:
        cause = f"the worker was refused {', '.join(result.denied[:3])}"
    else:
        cause = "the worker ran out of turns before checking its work"
    before, compared = _against_base(baseline, command, full, workdir) if _ran_and_failed(code, full) \
        else (False, "")
    if before:
        return dataclasses.replace(
            result, passed=True, failure=None, evidence=output,
            summary=f"{result.summary} ({cause}; `{command}` failed only as it does at the base, in its place)")
    if code != 0:
        why = "timed out" if code is None else f"exited {code}"
        evidence = f"`{command}` {why}:\n{output}".strip() + (f"\n\n{compared}" if compared else "")
        return dataclasses.replace(result, evidence=evidence, check_tail=_tail_of(full, workdir, baseline, compared),
                                   summary=f"{result.summary} ({cause}; locks ran `{command}`, which {why})")
    return dataclasses.replace(
        result, passed=True, failure=None, evidence=output or f"`{command}` exited 0",
        summary=f"{result.summary} ({cause}; `{command}` passed in its place)",
    )


#: What a worker says when the failure its check shows was there before its work: one pattern per
#: way of saying it, each kept small enough to read.
_BASE_CLAIM = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"pre-?existing|already (?:fail\w*|broken|red)",
    r"(?:fail\w*|red) (?:at|on|in) the base",
    r"(?:at|on) the base (?:commit|branch)",
    r"before (?:my|this|the) (?:change|work)",
    r"not (?:caused|introduced) by (?:my|this|the)",
    r"unrelated to (?:my|this|the) (?:change|work|task)",
))
#: The failures a worker's own account may be of a failure that was already there: not a refusal, not a turn limit.
_CLAIMS_BASE = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.INCONCLUSIVE, Failure.ENVIRONMENT})


def _claims_base(result: launch.WorkerResult) -> bool:
    """Whether a worker's report that did not pass says the check's failure was there before its
    work: a finding it marks `pre-existing`, or words that say so."""
    if any(f.get("cause") == "pre-existing" for f in result.findings):
        return True
    said = f"{result.summary}\n{result.evidence}"
    return any(pattern.search(said) for pattern in _BASE_CLAIM)


def _weigh_base_claim(result: launch.WorkerResult, command: str | None, workdir: Path,
                      baseline: Baseline | None, workspace: isolate.Workspace | None,
                      expected: tuple[str, ...] = ()) -> launch.WorkerResult:
    """A worker that wrote something and says the check fails as it did before its work: the claim is
    checked, never taken or dropped. locks runs the check and compares it with the base
    (`_against_base`): when every failing line was there too and every criterion the request lists
    (`expected`) is answered and met, the work passes; otherwise the worker's report stands as it was,
    with the comparison added for the next attempt to read. A check that passes, or cannot run, leaves
    the report alone, and so does an answer whose form failed (`shape`): with no single result block
    there is no report to weigh."""
    if not command or result.denied or result.shape or result.failure not in _CLAIMS_BASE \
            or not _claims_base(result):
        return result
    if not (isolate.has_changes(workspace) if workspace else bool(result.changed_paths)):
        return result
    if baseline is not None:
        baseline.found = None
    code, full = _steady_check(command, workdir, baseline)
    if not _ran_and_failed(code, full):
        return result
    before, compared = _against_base(baseline, command, full, workdir)
    left_out, not_met = criteria_mod.unmet(expected, result.criteria)
    if before and not left_out and not not_met and all(c.get("met") for c in result.criteria):
        return dataclasses.replace(
            result, passed=True, failure=None, verdict="pass", evidence=full[-CHECK_OUTPUT_KEPT:],
            summary=f"{result.summary} (locks: it said `{command}` failed before its work, and locks' own run "
                    "at the base shows the same failures, so the work passes as it stands)")
    if not compared:
        return result
    return dataclasses.replace(result, evidence=f"{result.evidence}\n\nwhat the worker said of the base, checked "
                                                f"by locks: {compared}".strip())


def _only_the_base(result: launch.WorkerResult, command: str | None, baseline: Baseline | None) -> bool:
    """A worker that stopped on a point (`spec_bug`, `architecture_bug`) whose point is the
    check failing on what already failed at the base: the check's failures were all there
    before the work (`baseline.found`), the worker names the check or one of those failures,
    and every criterion it answered is met. Anything else stays a decision."""
    if not command or baseline is None or not baseline.found:
        return False
    if any(not c.get("met") for c in result.criteria):
        return False
    said = f"{result.summary}\n{result.evidence}"
    failing = [str(line) for line in baseline.found.get("failures") or ()]
    return command in said or any(_named(line, said) for line in failing)


def _named(line: str, said: str) -> bool:
    """A failing line a worker's words name: its longest path or test name appears in them."""
    words = sorted((w for w in re.split(r"[\s:'\"()]+", line) if len(w) >= 6), key=len, reverse=True)
    return bool(words) and words[0] in said


def _stands(result: launch.WorkerResult, command: str | None, workdir: Path,
            baseline: Baseline | None = None) -> str:
    """Whether the work of a worker that reported a failure of the task (`spec_bug`,
    `architecture_bug`) stands anyway: "locks" when the repository's check, run by locks,
    passes on it; "worker" when there is none to run and the worker's own check is its
    evidence (shown, with at most the one criterion the open point leaves unmet); "" otherwise."""
    if command:
        if baseline is not None:
            baseline.found = None
        code, output = _steady_check(command, workdir, baseline)
        return "locks" if code == 0 or (_ran_and_failed(code, output) and _against_base(
            baseline, command, output, workdir)[0]) else ""
    unmet = sum(1 for c in result.criteria if not c.get("met"))
    return "worker" if result.evidence.strip() and unmet <= 1 and unmet < max(len(result.criteria), 1) else ""


def _note(store: Store, task: dict, key: str | None, report: Report, the_plan: Plan, checkout: Path,
          commit: str | None, ask: notes.Ask | None) -> None:
    """File what the passing worker learned in the project's notes, anchored to
    the files it changed as its branch has them. Never fails the run: the work
    passed, and a note that could not be filed is said in the report."""
    if not key:
        return
    lookups = [(a, s) for a, s in the_plan.active if hasattr(a, "symbols_named")]

    def symbols_of(fact: str) -> list[tuple[str, str]]:
        return [x for adapter, status in lookups for x in adapter.symbols_named(fact, status)]

    try:
        report.noted = notes.file_facts(
            store, key, report.learned, author="worker", source=f"task #{task['id']}", task_id=task["id"],
            repo_dir=checkout, paths=report.changed, commit=commit, symbols_of=symbols_of if lookups else None,
            ask=ask)
    except Exception as exc:  # a pass is not undone by its notes
        report.impact.append(f"notes: what the worker learned could not be filed ({type(exc).__name__}: {exc})")
        return
    if report.noted:
        store.add_event(task["id"], "noted", notes=report.noted)


def _to_engram(store: Store, task: dict, report: Report, the_plan: Plan, checkout: Path) -> None:
    """Give the team's engram what a passing worker learned, when the person turned
    it on. The facts are the ones notes would keep: never a secret. Never fails
    the run, and a save that failed is said in the report."""
    found = next(((a, s) for a, s in the_plan.active if a.name == "engram"), None)
    if found is None:
        report.impact.append("engram: what the worker learned was not saved (no verified engram server answered)")
        return
    adapter, status = found
    facts = [f for f in (notes.clean_fact(x) for x in report.learned) if f]
    try:
        saved = adapter.remember(facts, project=str(status.data.get("project") or ""), task_id=task["id"],
                                 title=task["title"], directory=checkout)
    except Exception as exc:  # a pass is not undone by a neighbour
        report.impact.append(f"engram: what the worker learned could not be saved ({type(exc).__name__}: {exc})")
        return
    if saved:
        report.impact.append(f"engram: saved {len(saved)} memories to {status.data.get('project')} "
                             f"(#{', #'.join(map(str, saved))})")
        store.add_event(task["id"], "engram_saved", project=status.data.get("project"), ids=saved)


def _solve_matched(store: Store, task: Mapping[str, Any], key: str | None, cell: Cell,
                   result: launch.WorkerResult) -> list[int]:
    """The open problems of this repository the passing work's own text matches strictly (as a
    dead end is matched): the work was about them, and its pass solves them (a later task that
    finishes what an earlier one diagnosed). Each gets a fix that worked, by this task. Never
    another repository's."""
    solved = []
    if key is None:
        return solved
    for problem in store.search(cells.memory_text(store, task), repo=key, limit=10, strict=True):
        problem_id = int(problem["id"])
        if problem["repo"] != key or problem["state"] == "solved" or any(
                f["task_id"] == task["id"] for f in problem["fixes"] if f["outcome"] == "worked"):
            continue
        store.add_fix(problem_id, f"{cell.label}: task #{task['id']}, whose text matched it, passed: "
                      f"{clip(result.summary, 300)}", "worked", repo=key, evidence=result.evidence,
                      task_id=task["id"], areas=cells.of_paths(result.changed_paths) or None)
        solved.append(problem_id)
    return solved


def _remember(store: Store, task: dict, key: str | None, attempt: Attempt, outcome: str,
              result: launch.WorkerResult, *, role: str | None, kind: str) -> None:
    """A fix kept with its cell: the role the worker wore and the areas of what it changed
    (of its request when it changed nothing), matched later by the work's own text."""
    goal = cells.memory_text(store, task)
    problem = store.open_problem(task["title"], repo=key, symptom=goal[:1000])
    if outcome == "failed":
        # Work about a problem that was solved here failing again: that fix did not hold.
        store.regressed(goal, repo=key, task_id=int(task["id"]), keep=problem)
    store.add_fix(
        problem, f"{attempt.cell.label}: {result.summary}", outcome, repo=key,
        why=str(attempt.failure or "") if outcome == "failed" else "",
        evidence=result.evidence, task_id=task["id"], role=role,
        areas=cells.of_paths(result.changed_paths) or cells.of_request(goal, kind),
    )
