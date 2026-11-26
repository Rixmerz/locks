"""Earned trust: what the independent check's record says of a cell, and how far it lets locks
look less and start cheaper.

A cell here is a repository, a kind and the cell a worker ran on. Each task's first independent
check of its work is one inspection (a lot, in acceptance sampling's words): clean, or blocked by a
problem the change caused. A correction's re-check is no new lot, and a check that could not decide
says nothing.

Sampling (`sampling`) is skip-lot sampling (ISO 2859-3): every pass is checked until `QUALIFY`
inspections in a row came clean; then one in two, and after another `QUALIFY` clean ones, one in
`EVERY[-1]`, never fewer. One blocker returns the cell to checking every pass, and it qualifies again
from zero. Only a change the verifier reads because a cheap cell made it (risk medium) is ever
sampled: a high risk is checked every time, and so are a fix of ledger rows, a correction's re-check
and a resume after a review stop (`orchestrate._sampled_out`). Which passes are checked is a hash of
the repository and the task, so a resume decides as its first run did and nothing is left to the
clock. A pass the sampling let through is a `verifier_sampled` event: its check said nothing, so it
neither qualifies a cell nor resets one, and locks' own check still ran on it.

The probation rung (`probation_arm`): a writing kind with a cheaper cell below its ladder
(`matrix.PROBATION`) tries it, in arms that alternate with the ladder's first rung, only where that
rung already passes `standards.PASS_FIRST` of its first tries here, and never on a high risk. The
trial's tries are first tries like any: `standards.calibrate` makes the cheaper cell the start only by
its own rule, and `standards.judge` reverts it when pass-first falls or a point that passed costs more
than on the rung it replaced.

What it says of learning (`report`): per cell, how often its checks came clean, where its sampling
stands, how many of its blockers repeat one an earlier task here already got, how often a
correction's re-check found something the round before had not, and pass-first and cost per point by
the cell a task started on. Read only, never by a model.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from locks import config, standards
from locks import findings as findings_mod
from locks.matrix import PROBATION, Cell, ladder_for
from locks.store import Store
from locks.text import words

#: Clean inspections in a row that qualify a cell for the next, sparser frequency: ISO 2859-3's
#: skip-lot plans qualify a product for reduced inspection after ten consecutive lots accepted.
QUALIFY = 10
#: How often a qualified cell's passes are checked: every one, then one in two, then one in four,
#: and never fewer, so the record that grants the trust keeps measuring it.
EVERY = (1, 2, 4)
#: A probation trial's tries of the cheaper cell before it ends and the calibration decides: as
#: many as a start needs to be chosen (`standards.MIN_SAMPLES`).
PROBATION_TRIAL = standards.MIN_SAMPLES
#: The trial's arms count from this many days back: one that ended, won or lost, may run again
#: once its tasks are older, as a race does once its races leave the window.
PROBATION_DAYS = 30
#: A trial stops early, lost, once the cheaper cell has this many first tries and passes them
#: worse than `standards.PASS_FIRST` by more than `standards.REVERT_MARGIN`.
PROBATION_EARLY = 3
#: Two blockers of different tasks are the same mistake when they name the same file and share at
#: least this share of their words: a fix moves lines and rewords (`findings.same`).
REPEAT_OVERLAP = 0.5


@dataclass(frozen=True)
class Inspection:
    """One task's first independent check (`clean` or `blocked`), or a pass its sampling let
    through unchecked (`sampled`), with the cell of the attempt it read."""

    task: int
    repo: str | None
    kind: str
    cell: str
    outcome: str
    ts: str
    blockers: tuple[Mapping[str, str], ...] = ()


def _cell_of(row: Mapping[str, Any]) -> str | None:
    label = (row["data"] or {}).get("worker") or row.get("attempt_cell")
    try:
        return Cell.parse(label).label if label else None
    except ValueError:
        return None


def inspections(store: Store, repo: str | None) -> list[Inspection]:
    """The inspections of a repository (None: of every one), oldest first."""
    out: list[Inspection] = []
    seen: set[int] = set()
    for row in store.verifier_record(repo):
        data = row["data"] or {}
        cell = _cell_of(row)
        if not cell:
            continue
        task = int(row["task_id"])
        if row["event"] == "verifier_sampled":
            out.append(Inspection(task, row["repo"], row["kind"] or "", cell, "sampled", row["ts"]))
            continue
        if int(data.get("round") or 0) or task in seen:
            continue
        outcome = "blocked" if int(data.get("blocking") or 0) else "clean" if data.get("passed") else None
        if outcome is None:
            continue
        seen.add(task)
        out.append(Inspection(task, row["repo"], row["kind"] or "", cell, outcome, row["ts"],
                              tuple(b for b in data.get("blockers") or () if isinstance(b, Mapping))))
    return out


@dataclass(frozen=True)
class Level:
    """Where a cell's sampling stands: one pass in `every` checked, and the clean inspections in a
    row at that frequency (`run`), with the counts it was read from."""

    every: int = 1
    run: int = 0
    checks: int = 0
    clean: int = 0
    blocked: int = 0
    sampled: int = 0

    def data(self) -> dict[str, int]:
        return {"every": self.every, "run": self.run, "checks": self.checks, "clean": self.clean,
                "blocked": self.blocked, "sampled": self.sampled, "qualify": QUALIFY}


def level(found: Sequence[Inspection]) -> Level:
    """Skip-lot sampling over a cell's inspections, in order: `QUALIFY` clean in a row move it one
    frequency sparser, one blocker back to every pass; a pass let through moves nothing."""
    at = run = checks = clean = blocked = sampled = 0
    for inspection in found:
        if inspection.outcome == "sampled":
            sampled += 1
            continue
        checks += 1
        if inspection.outcome == "blocked":
            blocked += 1
            at = run = 0
            continue
        clean += 1
        run += 1
        if run >= QUALIFY and at < len(EVERY) - 1:
            at, run = at + 1, 0
    return Level(EVERY[at], run, checks, clean, blocked, sampled)


def due(repo: str, task_id: int, every: int) -> bool:
    """Whether a task's pass is one its cell's sampling checks: a hash, so a resume decides alike."""
    if every <= 1:
        return True
    digest = hashlib.sha256(f"{repo}|{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % every == 0


@dataclass(frozen=True)
class Sampling:
    """Whether the independent check reads this pass, and why it may not."""

    check: bool
    every: int = 1
    why: str = ""
    level: Level = field(default_factory=Level)


def sampling(store: Store, repo: str | None, kind: str, cell: Cell, task_id: int, *, tier: str,
             env: Mapping[str, str] | None = None) -> Sampling:
    """Whether a pass the verifier is called for gets it. Every high risk does; so does every pass
    of a cell its record has not qualified (`level`), and one in `every` of a qualified cell's."""
    if not repo or tier == "high" or not config.enabled("trust", os.environ if env is None else env):
        return Sampling(True)
    record = level([i for i in inspections(store, repo) if i.kind == kind and i.cell == cell.label])
    if record.every == 1:
        return Sampling(True, 1, "", record)
    said = (f"{kind} work at {cell.label} here is checked one pass in {record.every}: its independent checks "
            f"came clean {QUALIFY} times in a row ({record.clean} of {record.checks} clean in all); a blocker "
            "returns it to every pass")
    if due(repo, task_id, record.every):
        return Sampling(True, record.every, f"sampled for its independent check: {said}", record)
    return Sampling(False, record.every, f"its independent check was not sampled: {said}", record)


# --- the probation rung ----------------------------------------------------------------------------

def probation_arm(store: Store, repo: str | None, kind: str, band: int, start: Cell, *, tier: str,
                  eligible: bool, env: Mapping[str, str] | None = None,
                  now: datetime | None = None) -> tuple[str | None, str]:
    """The arm of a probation trial a task starts in (`probation`: the cheaper cell, `ladder`: the
    first rung) and why; (None, "") outside a trial. A trial runs only where the ladder's first rung
    passes `standards.PASS_FIRST` of `standards.MIN_SAMPLES` or more first tries of the kind here
    (of its size, when its band holds that many), on a start nothing raised or pinned (`eligible`
    and `start`), never on a high risk, and until the cheaper cell was planned `PROBATION_TRIAL`
    times in `PROBATION_DAYS`; it stops early once that cell's first tries pass far worse
    (`PROBATION_EARLY`). The arm with fewer tasks goes next."""
    cell = PROBATION.get(kind)
    env = os.environ if env is None else env
    if (cell is None or not repo or not eligible or tier == "high" or not config.enabled("probation", env)
            or not config.enabled("verifier", env)):
        return None, ""
    first = ladder_for(kind)[0]
    if start != first:
        return None, ""
    mine = [t for t in standards.ended(store, repo=repo) if t.kind == kind]
    sized = [t for t in mine if t.band == band][-standards.WINDOW:]
    window = sized if len(sized) >= standards.MIN_SAMPLES else mine[-standards.WINDOW:]
    passed, tried = standards.rates(window).get(first.label, [0, 0])
    if tried < standards.MIN_SAMPLES or passed / tried < standards.PASS_FIRST:
        return None, ""
    since = ((now or datetime.now(UTC)) - timedelta(days=PROBATION_DAYS)).isoformat(timespec="seconds")
    arms = store.probation_arms(repo, kind, band, since=since)
    if arms["probation"] >= PROBATION_TRIAL:
        return None, ""
    won, tries = standards.rates([t for t in mine if t.ended_at >= since]).get(cell.label, [0, 0])
    if tries >= PROBATION_EARLY and won / tries < standards.PASS_FIRST - standards.REVERT_MARGIN:
        return None, ""
    evidence = f"{passed} of {tried} first tries at {first.label} passed here"
    if arms["probation"] <= arms["ladder"]:
        return "probation", (f"on trial at {cell.label}, below the {kind} ladder: {evidence}, so the cheaper cell "
                             f"is tried in turns with it ({arms['probation'] + 1} of {PROBATION_TRIAL}); its pass is "
                             "read by the independent check, and it becomes the start only if its first tries pass "
                             "as often")
    return "ladder", (f"the {kind} trial of {cell.label} runs in turns: this task starts at {first.label} "
                      f"({arms['probation']} of {PROBATION_TRIAL} tried at {cell.label} so far)")


# --- what the record says --------------------------------------------------------------------------

#: A token of code in a finding's words: what a reviewer names whatever language it writes in (`@spec`, a path,
#: `snake_case`, `camelCase`, `a.b`, a backticked name). Words of prose are a language's; these are the code's.
_CODE = re.compile(r"`([^`\s]{2,60})`|(@[\w-]+|[\w-]+/[\w./-]+|\w+_\w+|[a-z]+[A-Z]\w*|\w+\.\w+)")
#: Code tokens two blockers share, at least, to be one problem met again in another place.
REPEAT_CODES = 2


def _codes(text: str) -> set[str]:
    return {(a or b).lower().strip(".") for a, b in _CODE.findall(text)}


def _repeats(blocker: Mapping[str, str], earlier: Sequence[Mapping[str, str]]) -> bool:
    """Whether a blocker is one an earlier task already got: the same finding, the same place with most of its
    words, or the code it names shared (#207, B4: a verifier writing in two languages matched 0 of 143 by
    words alone)."""
    codes = _codes(str(blocker.get("what") or ""))
    for other in earlier:
        theirs = _codes(str(other.get("what") or ""))
        shared = codes & theirs
        same_place = findings_mod.place(str(blocker.get("where") or "")) and findings_mod.place(
            str(blocker.get("where") or "")) == findings_mod.place(str(other.get("where") or ""))
        if len(shared) >= REPEAT_CODES or (same_place and shared):
            return True
    mine = set(words(str(blocker.get("what") or "")))
    for other in earlier:
        if findings_mod.key(blocker) == findings_mod.key(other):  # type: ignore[arg-type]
            return True
        place = findings_mod.place(str(blocker.get("where") or ""))
        if not place or place != findings_mod.place(str(other.get("where") or "")):
            continue
        theirs = set(words(str(other.get("what") or "")))
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= REPEAT_OVERLAP:
            return True
    return False


def _rechecks(store: Store, repo: str | None) -> dict[tuple[str | None, str, str], list[int]]:
    """Per cell, [re-checks, the ones that found a problem the round before had not]."""
    out: dict[tuple[str | None, str, str], list[int]] = defaultdict(lambda: [0, 0])
    before: dict[int, list[Mapping[str, str]]] = defaultdict(list)
    for row in store.verifier_record(repo):
        data = row["data"] or {}
        if row["event"] != "verified" or data.get("late"):
            continue
        found = [b for b in data.get("blockers") or () if isinstance(b, Mapping)]
        task = int(row["task_id"])
        cell = _cell_of(row)
        if int(data.get("round") or 0) and cell:
            counts = out[(row["repo"], row["kind"] or "", cell)]
            counts[0] += 1
            counts[1] += any(not any(findings_mod.same(b, a) for a in before[task])  # type: ignore[arg-type]
                             for b in found if b.get("where") is not None and b.get("what") is not None)
        before[task] += [b for b in found if b.get("where") is not None and b.get("what") is not None]
    return out


def report(store: Store, repo: str | None) -> list[dict[str, Any]]:
    """Per repository, kind and cell: its inspections, how often they came clean, where its sampling
    stands, the blockers that repeat an earlier task's here, the correction re-checks and how many
    found something new, and pass-first and cost per point of the tasks that started on it."""
    found = inspections(store, repo)
    rechecks = _rechecks(store, repo)
    groups: dict[tuple[str | None, str, str], list[Inspection]] = defaultdict(list)
    for inspection in found:
        groups[(inspection.repo, inspection.kind, inspection.cell)].append(inspection)
    started: dict[tuple[str | None, str, str], list[standards.Ended]] = defaultdict(list)
    for task in standards.ended(store, repo=repo):
        tries = standards.samples(task.attempts) if not task.pinned else []
        if tries:
            started[(task.repo, task.kind, tries[0][0])].append(task)
    earlier: dict[str | None, list[Mapping[str, str]]] = defaultdict(list)
    repeated: dict[tuple[str | None, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for inspection in found:
        key = (inspection.repo, inspection.kind, inspection.cell)
        for blocker in inspection.blockers:
            repeated[key][0] += 1
            repeated[key][1] += _repeats(blocker, earlier[inspection.repo])
        earlier[inspection.repo] += list(inspection.blockers)
    rows = []
    for key in sorted({*groups, *rechecks, *started}, key=lambda k: (k[0] or "", k[1], k[2])):
        record = level(groups.get(key, []))
        measured = standards.metrics(started.get(key, [])[-standards.WINDOW:])
        blockers, again = repeated.get(key, [0, 0])
        checked, new = rechecks.get(key, [0, 0])
        rows.append({
            "repo": key[0], "kind": key[1], "cell": key[2], **record.data(),
            "clean_rate": round(record.clean / record.checks, 3) if record.checks else None,
            "blockers": blockers, "repeated": again,
            "repeat_rate": round(again / blockers, 3) if blockers else None,
            "rechecks": checked, "rechecks_new": new,
            "started": measured["tasks"], "pass_first": measured["pass_first"],
            "cost_per_point": measured["cost_per_point"],
        })
    return rows


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _name(repo: str | None) -> str:
    return re.sub(r"^.*/", "", repo or "?")


def text(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The report, a line per cell."""
    out = []
    for r in rows:
        checking = "every pass" if r["every"] == 1 else f"1 pass in {r['every']}"
        toward = f", {r['run']}/{QUALIFY} clean toward the next" if r["every"] < EVERY[-1] else ""
        point = "—" if r["cost_per_point"] is None else f"${r['cost_per_point']:.3f}"
        out.append(
            f"{_name(r['repo']):<16} {r['kind']:<12} {r['cell']:<14} checks {r['checks']:>3}  clean "
            f"{_rate(r['clean_rate']):>4}  checking {checking}{toward}  unchecked {r['sampled']:>3}  repeated "
            f"blockers {r['repeated']}/{r['blockers']}  re-checks finding new {r['rechecks_new']}/{r['rechecks']}  "
            f"started {r['started']:>3}  first {_rate(r['pass_first']):>4}  per point {point}")
    return out
