"""Where locks' time and money go, and what it gets for them: `locks stats` and
the UI's Analysis screen.

Read from the store only, never by a model. A run is one `locks run` of a task,
from its `planned` event to its `finished` one: a resumed task has one run per
resume. Each run's phases are on its events (queued, planning, setting up,
working, checking, verifying), and each attempt's cell, turns, tokens and wall
time. Events written before the phases were timed have no such fields: their
runs count by their timestamps, and a phase they never measured is left out of
the breakdown rather than guessed.

What it answers is what the routing needs evidence for: how often a kind passes
first time and how often it climbs, what a pass costs and how long it takes,
where the waiting is, whether the session's thread in a brief changes how a run
goes, and what a session's own turns take beside the work it sends.
"""
from __future__ import annotations

import csv
import io
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from locks import behavior, launch, racing, revisions, trunks, trust, usage, workflows
from locks import cells as cells_mod
from locks import critique as critique_mod
from locks import evidence as evidence_mod
from locks import methods as methods_mod
from locks import notes as notes_mod
from locks import predict as predict_mod
from locks import route as route_mod
from locks.store import Store

#: The phases of a run, in the order they happen. The first four come before its own wall
#: time: the engine queueing it after the step it waited on passed (`advance_s`), its wait
#: for the dispatcher (`pickup_s`) and the process starting (`spawn_s`), which split the
#: older `queued_s` where they were measured.
PHASES = ("advance_s", "pickup_s", "spawn_s", "queued_s", "plan_s", "setup_s", "worker_s", "check_s", "verifier_s",
          "wait_s", "notes_s", "lost_s", "unnoticed_s", "other_s")
#: The phases before a run's own start: never part of its wall time, so never taken from "other".
BEFORE_RUN = frozenset({"advance_s", "pickup_s", "spawn_s", "queued_s"})
#: The events a compaction decision leaves (`locks.compaction`).
COMPACTION_EVENTS = ("compaction_decided", "compaction_delivered", "compaction_settled", "compaction_seen",
                     "compaction_continued")
#: How many compaction decisions the text lists, newest last.
COMPACTIONS_SHOWN = 10
#: The columns of a run, as `locks stats --runs` writes them.
RUN_COLUMNS = ("task", "repo", "kind", "source", "workflow", "step", "status", "stop", "start_cell", "final_cell",
               "started_at", "ended_at", "wall_s", *PHASES[:-1], "attempts", "first_try", "verified",
               "verifier_blocked", "cost_usd", "turns", "input_tokens", "output_tokens", "tokens",
               "livespec_handed", "livespec_calls", "calls", "failed_calls", "check_runs", "fix_cycles",
               "calls_before_edit", "session_context", "session_chars", "brief_chars", "title")
#: One row per attempt (`locks stats --attempts`): what it ran at, how it ended, and its worker's profile.
ATTEMPT_COLUMNS = ("task", "seq", "repo", "kind", "ts", "cell", "served_model", "passed", "failure", "turns",
                   "wall_s", "check_s", "verifier_s", "cost_usd", "worker_cost_usd", "input_tokens",
                   "output_tokens", "cache_read_tokens", "continued", "refused", *behavior.FIELDS, "tools")


def _when(stamp: str | None) -> datetime | None:
    """A stored time; one written without a zone is UTC, as locks writes them."""
    try:
        found = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _between(start: str | None, end: str | None) -> float | None:
    a, b = _when(start), _when(end)
    return round((b - a).total_seconds(), 1) if a and b and b >= a else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def pct(values: Iterable[float | None], q: float) -> float | None:
    """The q-th percentile (0..1) by nearest rank; None with no values."""
    found = sorted(v for v in values if v is not None)
    if not found:
        return None
    return round(found[min(len(found) - 1, max(0, math.ceil(q * len(found)) - 1))], 2)


def runs(tasks: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each run of each task, with its phases, attempts, cost and outcome."""
    by_task: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        by_task[int(event["task_id"])].append(event)
    out = []
    for task in tasks:
        current: dict[str, Any] | None = None
        for event in by_task.get(int(task["id"]), ()):
            kind, data = event["kind"], event["data"] or {}
            if kind == "planned":
                current = _start(task, event)
                continue
            if current is None:
                continue
            if kind == "briefed":
                session = data.get("session")
                current["session_context"] = bool(session)
                current["session_chars"] = (session or {}).get("chars", 0)
                if data.get("livespec_chars"):
                    current["dossier_chars"] = max(current["dossier_chars"] or 0, int(data["livespec_chars"]))
            elif kind == "attempt_started":
                # An attempt with no `attempt_finished` before the run's end lost its worker (it died,
                # or was cut off): the time from its start to the end is lost, not "other".
                current["_open_attempt"] = event["ts"]
                current["_open_turns"] = data.get("max_turns")
                if "livespec" in (data.get("capabilities") or ()):
                    current["livespec_handed"] = True
                if current["brief_chars"] is None:
                    current["brief_chars"] = data.get("brief_chars")
                    if (since := _number(data.get("since_start_s"))) is not None:
                        current["setup_s"] = round(max(since - (current["plan_s"] or 0), 0), 2)
            elif kind == "attempt_lost":
                # A worker that died never wrote its result: what its transcript says it spent (since 0.80).
                current["lost_usd"] = round(current["lost_usd"] + (_number(data.get("cost_usd")) or 0), 4)
                current["cost_usd"] += _number(data.get("cost_usd")) or 0
            elif kind == "attempt_finished":
                current["_open_attempt"] = None
                _attempt(current, data)
            elif kind == "verified":
                current["_check_usd"] += _number(data.get("cost_usd")) or 0
                current["verified"] += 1
                current["verifier_blocked"] += 0 if data.get("passed") else 1
                current["_verifier_s"] += _number(data.get("wall_s")) or _number(data.get("duration_s")) or 0
            elif kind == "moved":
                current["moves"].append(data.get("move"))
            elif kind == "finished":
                out.append(_end(current, event))
                current = None
    return out


def _start(task: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    data = event["data"] or {}
    return {
        "task": task["id"], "repo": task.get("repo"), "title": task.get("title") or "",
        "kind": data.get("kind") or task.get("kind"), "source": task.get("source"),
        "workflow": task.get("workflow"), "step": task.get("workflow_step"),
        "has_session": bool(task.get("session_id")), "fan_part": _fan_part(task), "last_failure": None,
        "start_cell": data.get("start"), "final_cell": None, "status": None, "stop": None,
        "started_at": event["ts"], "ended_at": None, "wall_s": None,
        "advance_s": None, "pickup_s": None, "spawn_s": None,
        "queued_s": _number(data.get("queued_s")), "plan_s": _number(data.get("plan_s")), "setup_s": None,
        "worker_s": None, "check_s": None, "verifier_s": None, "wait_s": None, "hold_s": None, "notes_s": None,
        "lost_s": None, "unnoticed_s": None, "_open_attempt": None, "_open_turns": None,
        "attempts": 0, "first_try": None, "verified": 0, "verifier_blocked": 0, "moves": [], "cells": [],
        "cost_usd": 0.0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "refused": 0,
        "cache_read_tokens": 0, "measured_input_tokens": 0, "verifier_cost_usd": 0.0, "tokens": 0,
        "livespec_handed": False, "livespec_calls": 0, "livespec_tools": {}, "dossier_chars": None, "lost_usd": 0.0,
        "calls": 0, "failed_calls": 0, "check_runs": 0, "fix_cycles": 0, "calls_before_edit": None,
        "session_context": None, "session_chars": 0, "brief_chars": None,
        "_worker_s": 0.0, "_check_s": 0.0, "_verifier_s": 0.0, "_timed": False, "_check_usd": 0.0,
    }


def _fan_part(task: Mapping[str, Any]) -> bool:
    """Whether the task was queued as a part of a plan's fan-out (`workflows.advance`: the `part` in its
    options carries the `plan` it came from; one area of a blind review does not)."""
    try:
        part = json.loads(task.get("options") or "{}").get("part")
    except ValueError:
        return False
    return isinstance(part, dict) and bool(part.get("plan"))


#: How a part stops on a point: the stop causes and the attempt failures that name one.
POINT_STOPS = ("decision", "spec")
POINT_FAILURES = ("spec_bug", "architecture_bug")


def parts_on_a_point(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per repository, the parts of a fan-out that ended and how many stopped on a point (their stop cause
    or the failure of their last attempt): a plan whose parts leave a design choice to the worker."""
    count: dict[str | None, list[int]] = defaultdict(lambda: [0, 0])
    for r in found:
        if r.get("fan_part"):
            row = count[r["repo"]]
            row[1] += 1
            row[0] += r["stop"] in POINT_STOPS or r.get("last_failure") in POINT_FAILURES
    return [{"repo": repo, "stopped": n, "parts": total} for repo, (n, total) in sorted(count.items(), key=str)]


def _attempt(run: dict[str, Any], data: Mapping[str, Any]) -> None:
    run["attempts"] += 1
    run["last_failure"] = None if data.get("passed") else data.get("failure")
    if run["first_try"] is None:
        run["first_try"] = bool(data.get("passed"))
    run["cells"].append(data.get("cell"))
    run["cost_usd"] += _number(data.get("cost_usd")) or 0
    run["turns"] += int(data.get("turns") or 0)
    run["input_tokens"] += int(data.get("input_tokens") or 0)
    run["output_tokens"] += int(data.get("output_tokens") or 0)
    run["tokens"] = run["input_tokens"] + run["output_tokens"]
    for tool, n in (data.get("livespec_calls") or {}).items():
        run["livespec_tools"][tool] = run["livespec_tools"].get(tool, 0) + int(n)
    run["livespec_calls"] = sum(run["livespec_tools"].values())
    worked = data.get("behavior") or {}
    for field, into in (("calls", "calls"), ("failed", "failed_calls"), ("check_runs", "check_runs"),
                        ("fix_cycles", "fix_cycles")):
        run[into] += int(worked.get(field) or 0)
    if run["attempts"] == 1:
        run["calls_before_edit"] = worked.get("calls_before_edit")
    if "cache_read_tokens" in data:  # recorded since 0.18: older attempts say nothing of the cache
        run["cache_read_tokens"] += int(data.get("cache_read_tokens") or 0)
        run["measured_input_tokens"] += int(data.get("input_tokens") or 0)
    run["verifier_cost_usd"] += _split(data, run["_check_usd"])[1]
    run["_check_usd"] = 0.0
    run["refused"] += bool(data.get("denied"))
    wall = _number(data.get("wall_s"))
    run["_timed"] = run["_timed"] or wall is not None
    run["_worker_s"] += wall if wall is not None else (_number(data.get("duration_s")) or 0)
    run["_check_s"] += _number(data.get("check_s")) or 0


def _end(run: dict[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    data = event["data"] or {}
    run["ended_at"] = event["ts"]
    run["status"] = data.get("status")
    run["stop"] = data["stop"].get("cause") if isinstance(data.get("stop"), dict) else None
    run["final_cell"] = data.get("final_cell")
    run["wall_s"] = _number(data.get("wall_s")) or _between(run["started_at"], run["ended_at"])
    # The run's own account of its cost wins: it holds the classification and the checks.
    run["cost_usd"] = round(_number(data.get("cost_usd")) or run["cost_usd"], 4)
    for phase in ("worker_s", "check_s", "verifier_s", "wait_s", "hold_s", "advance_s", "pickup_s", "spawn_s",
                  "notes_s"):
        run[phase] = _number(data.get(phase))
    run["pickup_why"] = {k: float(data[k]) for k in PICKUP_CAUSES if _number(data.get(k))}
    if run["_open_attempt"]:
        # A worker runs at most its attempt's time limit: what passed after it, the run
        # waited dead for someone to notice (a sweep at the next session), not work lost.
        gone = _between(run["_open_attempt"], run["ended_at"])
        turns = run["_open_turns"] if isinstance(run["_open_turns"], int) else 0
        limit = float(launch.timeout_for(turns))
        if gone is not None:
            run["lost_s"] = round(min(gone, limit), 2)
            run["unnoticed_s"] = round(max(gone - limit, 0.0), 2)
    # A run from before the phases were timed: what its attempts measured, the CLI's
    # own duration for the work, and nothing for what was never measured.
    if run["worker_s"] is None and run["_worker_s"]:
        run["worker_s"] = round(run["_worker_s"], 2)
    if run["check_s"] is None and run["_timed"]:
        run["check_s"] = round(run["_check_s"], 2)
    if run["verifier_s"] is None and (run["_timed"] or run["_verifier_s"]):
        run["verifier_s"] = round(run["_verifier_s"], 2)
    return {k: v for k, v in run.items() if not k.startswith("_")}


#: What a pickup waited on (`orchestrate.pickup_causes`), as `locks stats` names each.
PICKUP_CAUSES = ("pickup_conflict_s", "pickup_limit_s", "pickup_ceiling_s", "pickup_level_s")
PICKUP_WORDS = {"pickup_conflict_s": "work ahead it was judged to wait for", "pickup_limit_s": "the usage limit",
                "pickup_ceiling_s": "your ceiling of workers (chosen)", "pickup_level_s": "the machine's level"}


def pickup(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The pickup of the runs that measured why it waited, by cause; the rest of the pickup
    (runs from before causes were kept) apart, never guessed."""
    seconds = dict.fromkeys(PICKUP_CAUSES, 0.0)
    told = unknown = 0.0
    for r in found:
        why, whole = r.get("pickup_why") or {}, r.get("pickup_s") or 0.0
        for k, v in why.items():
            seconds[k] += v
        told += sum(why.values())
        unknown += max(whole - sum(why.values()), 0.0) if not why else 0.0
    return {"seconds": {k: round(v, 1) for k, v in seconds.items()}, "unknown_s": round(unknown, 1),
            "share": {k: round(v / told, 3) if told else 0.0 for k, v in seconds.items()}}


def capability_use(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Attempts handed a capability, how many called it, and the pass rate of those that did and
    those that did not. Observed: the two groups differ in more than the calls."""
    used = [r for r in rows if r["calls"]]
    idle = [r for r in rows if not r["calls"]]

    def rate(rs: Sequence[Mapping[str, Any]]) -> float | None:
        return round(sum(r["passed"] for r in rs) / len(rs), 3) if rs else None

    return {"given": len(rows), "used": len(used), "calls": sum(r["calls"] for r in rows),
            "passed_used": rate(used), "passed_idle": rate(idle)}


def dossier_effect(found: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Runs whose first brief carried a dossier from livespec's index (`briefed.livespec_chars`) against
    those that did not: runs, first tries passed and the median wall time of a pass. Observed, never a
    trial: the two groups differ in more than the dossier (an index that was absent, a repository's age)."""
    out: dict[str, dict[str, Any]] = {}
    for name, group in (("with", [r for r in found if r.get("dossier_chars")]),
                        ("without", [r for r in found if not r.get("dossier_chars")])):
        first = [r for r in group if r["first_try"] is not None]
        took = [r["wall_s"] for r in group if r["status"] == "done" and r["wall_s"] is not None]
        out[name] = {"runs": len(group), "tries": len(first), "passed": sum(bool(r["first_try"]) for r in first),
                     "rate": _share(sum(bool(r["first_try"]) for r in first), len(first)),
                     "pass_p50_s": statistics.median(took) if took else None,
                     "chars_p50": statistics.median([r["dossier_chars"] for r in group]) if name == "with" and group
                     else None}
    return out


def livespec_tokens(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per kind, the tokens a run spent (input and output, its attempts together) when its workers called
    livespec, against when they did not: runs, mean and median of each, and how far the mean moved. A run
    that spent none is left out. Observed, not a trial: the two groups differ in more than the calls."""
    rows = []
    for kind in sorted({r["kind"] for r in found if r.get("kind")}):
        spent = [r for r in found if r.get("kind") == kind and r.get("tokens")]
        arms = {"with": [r["tokens"] for r in spent if r.get("livespec_calls")],
                "without": [r["tokens"] for r in spent if not r.get("livespec_calls")]}
        if not spent:
            continue
        row: dict[str, Any] = {"kind": kind}
        for arm, values in arms.items():
            row[arm] = {"runs": len(values), "avg": round(sum(values) / len(values)) if values else None,
                        "p50": pct(values, 0.5)}
        both = row["with"]["avg"] is not None and row["without"]["avg"]
        row["change"] = round((row["with"]["avg"] - row["without"]["avg"]) / row["without"]["avg"], 3) if both else None
        rows.append(row)
    return rows


def phases(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Seconds per phase over the runs whose phases were measured, and each one's share."""
    timed = [r for r in found if r["wall_s"] is not None and r["plan_s"] is not None]
    totals = dict.fromkeys(PHASES, 0.0)
    for r in timed:
        known = 0.0
        # A run whose queue time was split into pickup and spawn is not counted twice.
        split = r.get("pickup_s") is not None or r.get("spawn_s") is not None
        for phase in PHASES[:-1]:
            value = 0.0 if phase == "queued_s" and split else r.get(phase) or 0.0
            totals[phase] += value
            known += value if phase not in BEFORE_RUN else 0.0
        totals["other_s"] += max((r["wall_s"] or 0) - known, 0.0)
    whole = sum(totals.values())
    return {"runs": len(timed), "seconds": {k: round(v, 1) for k, v in totals.items()},
            "share": {k: round(v / whole, 3) if whole else 0.0 for k, v in totals.items()}}


def _waited(r: Mapping[str, Any]) -> float | None:
    """A run's wait before it started: its pickup and spawn where they were measured apart, else
    its queued time."""
    if r.get("pickup_s") is None and r.get("spawn_s") is None:
        return r.get("queued_s")
    return (r.get("pickup_s") or 0.0) + (r.get("spawn_s") or 0.0)


def _summary(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    passed = [r for r in found if r["status"] == "done"]
    first = [r for r in found if r["first_try"] is not None]
    cost = sum(r["cost_usd"] for r in found)
    wall = [r["wall_s"] for r in found]
    hours = sum(w for w in wall if w) / 3600
    return {
        "runs": len(found), "tasks": len({r["task"] for r in found}), "passed": len(passed),
        "pass_rate": round(len(passed) / len(found), 3) if found else None,
        "first_try_rate": round(sum(r["first_try"] for r in first) / len(first), 3) if first else None,
        "climbed_rate": round(sum(r["attempts"] > 1 for r in found) / len(found), 3) if found else None,
        "attempts_avg": round(sum(r["attempts"] for r in found) / len(found), 2) if found else None,
        "wall_p50_s": pct(wall, 0.5), "wall_p90_s": pct(wall, 0.9),
        "queued_p50_s": pct((_waited(r) for r in found), 0.5),
        "cost_usd": round(cost, 2), "cost_per_pass_usd": round(cost / len(passed), 2) if passed else None,
        "cost_p50_usd": pct((r["cost_usd"] for r in found), 0.5),
        "hours": round(hours, 2), "minutes_per_pass": round(hours * 60 / len(passed), 1) if passed else None,
        "verified_rate": round(sum(r["verified"] > 0 for r in found) / len(found), 3) if found else None,
        "verifier_cost_usd": round(sum(r["verifier_cost_usd"] for r in found), 2),
        "cache_read_share": _share(sum(r["cache_read_tokens"] for r in found),
                                   sum(r["measured_input_tokens"] for r in found)),
        "refused_rate": round(sum(r["refused"] > 0 for r in found) / len(found), 3) if found else None,
    }


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 3) if whole else None


def _split(data: Mapping[str, Any], checks_usd: float) -> tuple[float, float]:
    """An attempt's own cost and its independent check's. Kept apart since 0.18;
    before, its `cost_usd` held both, and its `verified` events (`checks_usd`, the
    ones since the attempt before) said what the checks cost."""
    if "worker_cost_usd" in data:
        return _number(data["worker_cost_usd"]) or 0, _number(data.get("verifier_cost_usd")) or 0
    total = _number(data.get("cost_usd")) or 0
    return round(max(total - checks_usd, 0), 4), round(min(checks_usd, total), 4)


def _grouped(found: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in found:
        groups[str(r.get(key) or "—")].append(r)
    return sorted(({key: name, **_summary(rows)} for name, rows in groups.items()),
                  key=lambda row: (-row["runs"], row[key]))


def routing(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per route mode: how often it was advised, and how often the session sent locks work
    after it. A `main` advice followed by work, or a `worker` one with none, is the session
    overriding it: the evidence a route is calibrated by (issue #124)."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        row = out.setdefault(str(r.get("mode") or "?"), {"advised": 0, "sent": 0})
        row["advised"] += 1
        row["sent"] += int(bool(r.get("sent")))
    for row in out.values():
        row["sent_rate"] = _share(row["sent"], row["advised"])
    return dict(sorted(out.items()))


def heavy_reads(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The prompts told that the turn before read heavily with the session's own tools
    (`routed.read_said`), and how often the session sent locks work after one."""
    said = [r for r in rows if r.get("read_said")]
    if not said:
        return None
    sent = sum(bool(r.get("sent")) for r in said)
    return {"advised": len(said), "sent": sent, "sent_rate": _share(sent, len(said))}


def notes_relied_on(events: Sequence[Mapping[str, Any]], task_ids: set[int]) -> dict[str, Any] | None:
    """What the notes in briefs were worth to the workers, in their words (`attempt_finished.notes_used`)
    against what each first brief carried (`notes.BRIEFED`): the tasks briefed with notes, the
    notes they carried, the ones a worker said it relied on, and how often first tries passed when one was
    named against when none was. Observed, never a trial and never a verdict; None when no brief had notes."""
    briefed: dict[int, set[int]] = {}
    used: dict[int, set[int]] = defaultdict(set)
    first: dict[int, bool] = {}
    said: set[int] = set()
    unsaid: set[int] = set()
    for e in events:
        tid = int(e["task_id"])
        if tid not in task_ids:
            continue
        data = e["data"] or {}
        if e["kind"] == "briefed" and tid not in briefed and data.get(notes_mod.BRIEFED):
            briefed[tid] = {int(n) for n in data[notes_mod.BRIEFED]}
        elif e["kind"] == "attempt_finished":
            used[tid] |= {int(n) for n in data.get("notes_used") or ()}
            if data.get("notes_unsaid"):
                unsaid.add(tid)
            elif "notes_used" in data:
                said.add(tid)
            first.setdefault(tid, bool(data.get("passed")))
    if not briefed:
        return None
    named = [t for t in briefed if used[t]]
    silent = [t for t in briefed if not used[t]]
    carried = sum(len(ids) for ids in briefed.values())
    relied = sum(len(used[t] & briefed[t]) for t in briefed)
    return {"tasks": len(briefed), "carried": carried, "relied_on": relied, "relied_rate": _share(relied, carried),
            "tasks_naming": len(named),
            # Tasks whose workers left `notes_used` out (since 0.80): no answer, never "none relied on".
            "tasks_unsaid": len({t for t in briefed if t in unsaid and t not in said}),
            "pass_first_naming": _share(sum(first.get(t, False) for t in named), len(named)),
            "pass_first_silent": _share(sum(first.get(t, False) for t in silent), len(silent))}


def capabilities_used(events: Sequence[Mapping[str, Any]], task_ids: set[int]) -> list[dict[str, Any]]:
    """Per capability a worker was handed (`attempt_finished.capability_calls`): the attempts that had
    it, the ones that called it at all and the ones whose calls went through, and how often the
    attempts that called it passed against the ones that did not. Observed, never a trial."""
    rows: dict[str, dict[str, int]] = defaultdict(lambda: {"handed": 0, "called": 0, "through": 0,
                                                           "called_passed": 0, "silent_passed": 0, "unlogged": 0})
    for e in events:
        if e["kind"] != "attempt_finished" or int(e["task_id"]) not in task_ids:
            continue
        for name, made in (e["data"].get("capability_calls") or {}).items():
            row = rows[str(name)]
            if e["data"].get("tools_logged") is False:
                # Its worker's calls never reached the log (`tools_logged`): not recorded, not zero.
                row["unlogged"] += 1
                continue
            row["handed"] += 1
            called = int((made or {}).get("calls") or 0) > 0
            row["called"] += int(called)
            row["through"] += int(int((made or {}).get("ok") or 0) > 0)
            row["called_passed" if called else "silent_passed"] += int(bool(e["data"].get("passed")))
    out = []
    for name, row in sorted(rows.items()):
        silent = row["handed"] - row["called"]
        out.append({"capability": name, **row, "never_called": silent,
                    "called_rate": _share(row["called"], row["handed"]),
                    "pass_when_called": _share(row["called_passed"], row["called"]),
                    "pass_when_silent": _share(row["silent_passed"], silent)})
    return out


def reviews_unread(store: Store, task_ids: set[int], *, since: str | None = None) -> dict[str, int]:
    """The reviews held back for reporting no finding while their calls show they read nothing
    (`review_unread` events, which the run's events do not carry): a worker's answer asked again
    (`worker`) and the second such answer (`worker_again`), an independent check run once more
    (`verifier`) and the one that stayed unread (`verifier_again`). Observed, never a rule's input."""
    found: Counter[tuple[str, bool]] = Counter()
    for event in store.events_of_kinds(["review_unread"], since=since):
        if int(event["task_id"]) in task_ids:
            found[(str(event["data"].get("who") or ""), bool(event["data"].get("again")))] += 1
    return {"worker": found[("worker", False)], "worker_again": found[("worker", True)],
            "verifier": found[("verifier", False)], "verifier_again": found[("verifier", True)]}


def unread_lines(held: Mapping[str, int]) -> list[str]:
    """`locks stats`' line of the reviews held back as unread; none while none was."""
    if not any(held.values()):
        return []
    return ["", "reviews held back for reporting no finding and reading nothing (their calls show no read): "
            f"workers held {held['worker']} ({held['worker_again']} unread again), "
            f"independent checks run again {held['verifier']} ({held['verifier_again']} unread twice)"]


def cells(events: Sequence[Mapping[str, Any]], task_ids: set[int]) -> list[dict[str, Any]]:
    """Every attempt by the cell it ran at: how often it passed, its time, turns, its own
    cost (its independent check's apart) and how much of its input the cache served."""
    by_cell: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    checks: dict[int, float] = defaultdict(float)
    for e in events:
        task = int(e["task_id"])
        if task not in task_ids:
            continue
        if e["kind"] == "verified":
            checks[task] += _number(e["data"].get("cost_usd")) or 0
        elif e["kind"] == "attempt_finished":
            own, check = _split(e["data"], checks.pop(task, 0.0))
            by_cell[str(e["data"].get("cell") or "—")].append({**e["data"], "_own": own, "_check": check})
    out = []
    for cell, rows in by_cell.items():
        out.append({
            "cell": cell, "attempts": len(rows), "passed": sum(bool(d.get("passed")) for d in rows),
            "wall_p50_s": pct((_number(d.get("wall_s")) or _number(d.get("duration_s")) for d in rows), 0.5),
            "turns_p50": pct((_number(d.get("turns")) for d in rows), 0.5),
            "cost_p50_usd": pct((d["_own"] for d in rows), 0.5),
            "cost_usd": round(sum(d["_own"] for d in rows), 2),
            "verifier_cost_usd": round(sum(d["_check"] for d in rows), 2),
            "cache_read_share": _share(sum(int(d.get("cache_read_tokens") or 0) for d in rows),
                                       sum(int(d.get("input_tokens") or 0) for d in rows if "cache_read_tokens" in d)),
            "failures": dict(Counter(d.get("failure") for d in rows if not d.get("passed") and d.get("failure"))),
        })
    return sorted(out, key=lambda row: -row["attempts"])


#: The moves whose next attempt may go on in the last one's session.
CONTINUING_MOVES = ("more_effort", "more_turns")


def continuations(events: Sequence[Mapping[str, Any]], task_ids: set[int]) -> list[dict[str, Any]]:
    """The attempts after more effort or more turns, by whether they went on in the attempt
    before's session or started fresh: how often each passed, its turns and its own cost.
    An attempt says it went on (`continued`, or `resumed` on its start, as locks first kept
    it); one that says neither started fresh, as every one did before sessions were kept."""
    last_move: dict[int, str | None] = {}
    resumed: set[tuple[int, Any]] = set()
    checks: dict[int, float] = defaultdict(float)
    groups: dict[tuple[str, bool], list[tuple[Mapping[str, Any], float]]] = defaultdict(list)
    for e in events:
        task, data = int(e["task_id"]), e["data"] or {}
        if task not in task_ids:
            continue
        if e["kind"] in ("planned", "moved"):
            last_move[task] = data.get("move")  # a new run's first attempt follows no move
        elif e["kind"] == "attempt_started" and data.get("resumed"):
            resumed.add((task, data.get("seq")))
        elif e["kind"] == "verified":
            checks[task] += _number(data.get("cost_usd")) or 0
        elif e["kind"] == "attempt_finished":
            own = _split(data, checks.pop(task, 0.0))[0]
            move = last_move.pop(task, None)
            if move in CONTINUING_MOVES:
                went_on = data["continued"] if "continued" in data else (task, data.get("seq")) in resumed
                groups[(str(move), bool(went_on))].append((data, own))
    return [{"move": move, "continued": went_on, "attempts": len(rows),
             "passed": sum(bool(d.get("passed")) for d, _ in rows),
             "turns_p50": pct((_number(d.get("turns")) for d, _ in rows), 0.5),
             "cost_p50_usd": pct((own for _, own in rows), 0.5), "cost_usd": round(sum(own for _, own in rows), 2)}
            for (move, went_on), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], not kv[0][1]))]


def context(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Runs a session sent, with the session's thread in their brief and without:
    the evidence on whether the scribe's context helps (observed, not a trial)."""
    sent = [r for r in found if r["has_session"] and r["session_context"] is not None]
    groups = (("with the session's thread", [r for r in sent if r["session_context"]]),
              ("without it", [r for r in sent if not r["session_context"]]))
    return [{"group": name, **_summary(rows)} for name, rows in groups if rows]


def turns(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A session's own turns and its subagents' delegations: how many, how long, what they cost."""
    tasks: dict[int, dict[str, Any]] = {}
    for row in rows:
        task = tasks.setdefault(int(row["id"]), {"source": row["source"], "status": row["status"],
                                                 "seconds": _between(row["created_at"], row["updated_at"]),
                                                 "usd": 0.0, "day": str(row["created_at"])[:10]})
        if row.get("model"):
            task["usd"] += usage.dollars(row["model"], int(row["input_tokens"] or 0), int(row["output_tokens"] or 0),
                                         int(row["cache_read"] or 0), int(row["cache_write"] or 0))
    out = {}
    for source, name in (("hook", "turns"), ("delegation", "delegations")):
        mine = [t for t in tasks.values() if t["source"] == source]
        ended = [t for t in mine if t["status"] == "done"]
        out[name] = {
            "count": len(mine), "done": len(ended), "interrupted": sum(t["status"] == "interrupted" for t in mine),
            "seconds_p50": pct((t["seconds"] for t in ended), 0.5),
            "seconds_p90": pct((t["seconds"] for t in ended), 0.9),
            "usd": round(sum(t["usd"] for t in mine), 2), "usd_p50": pct((t["usd"] for t in mine if t["usd"]), 0.5),
        }
    out["by_day"] = dict(Counter(t["day"] for t in tasks.values() if t["source"] == "hook"))
    return out


def daily(found: Sequence[Mapping[str, Any]], turn_days: Mapping[str, int]) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "passed": 0, "cost_usd": 0.0, "hours": 0.0})
    for r in found:
        day = days[str(r["ended_at"] or r["started_at"])[:10]]
        day["runs"] += 1
        day["passed"] += r["status"] == "done"
        day["cost_usd"] += r["cost_usd"]
        day["hours"] += (r["wall_s"] or 0) / 3600
    for day, count in turn_days.items():
        days[day]["turns"] = count
    return [{"day": d, "turns": v.get("turns", 0), "runs": v["runs"], "passed": v["passed"],
             "cost_usd": round(v["cost_usd"], 2), "hours": round(v["hours"], 2)} for d, v in sorted(days.items())]


def compactions(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each compaction decision (`locks.compaction`), oldest first: why, what the
    summary would cost with the cache warm beside a cold one, how it was
    delivered, and whether it ran (as settled, or as its marker was seen)."""
    from locks import compaction

    rows: dict[int, dict[str, Any]] = {}
    for event in events:
        data = event["data"]
        if event["kind"] == compaction.DECIDED:
            warm, cold = _number(data.get("warm_usd")), _number(data.get("cold_usd"))
            tokens, window = data.get("tokens") or 0, data.get("window") or compaction.WINDOW
            rows[event["id"]] = {
                "decision": event["id"], "at": event["ts"], "session": event.get("session_id"),
                "repo": event.get("repo"), "now": bool(data.get("now")), "mode": data.get("mode"),
                "tokens": tokens, "share": round(tokens / window, 2) if window else None,
                "because": data.get("because") or [], "warm_usd": warm, "cold_usd": cold,
                "warm_share": round(warm / cold, 2) if warm is not None and cold else None,
                "delivered": None, "ran": None, "evidence": None,
                "then": bool(data.get("then")), "continued": False}
            continue
        row = rows.get(int(data.get("decision") or data.get("marker") or 0))
        if row is None:
            continue
        if event["kind"] == compaction.DELIVERED:
            row["delivered"] = data.get("how")
        elif event["kind"] == compaction.SETTLED:
            row["ran"], row["evidence"] = bool(data.get("ran")), data.get("evidence")
        elif event["kind"] == compaction.CONTINUED:
            row["continued"] = True
        elif event["kind"] == compaction.SEEN and row["ran"] is None:
            row["ran"], row["evidence"] = True, f"{data.get('source')} carried its marker"
    return list(rows.values())


def compute(store: Store, *, days: int = 30, repo: str | None = None, keep_runs: int | None = 20) -> dict[str, Any]:
    """Everything the screen and the command show, for the last `days` (0: all) of
    one repository (None: every one). `keep_runs` slowest and costliest runs are
    listed; None lists every run."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds") if days else ""
    tasks, events = store.runs_since(since, repo=repo)
    found = [r for r in runs(tasks, events) if not since or (r["ended_at"] or "") >= since]
    turn_rows = turns(store.turns_since(since, repo=repo))
    stops = Counter(r["stop"] or r["status"] for r in found if r["status"] != "done")
    moves = Counter(m for r in found for m in r["moves"] if m)
    report = {
        "days": days, "repo": repo, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": _summary(found), "phases": phases(found), "pickup": pickup(found),
        "by_kind": _grouped(found, "kind"),
        "by_source": _grouped(found, "source"),
        "by_workflow": _grouped([r for r in found if r["workflow"]], "workflow"),
        "cells": cells(events, {r["task"] for r in found}), "context": context(found),
        "continuations": continuations(events, {r["task"] for r in found}),
        "stops": dict(stops.most_common()), "moves": dict(moves.most_common()),
        "turns": {k: v for k, v in turn_rows.items() if k != "by_day"},
        "daily": daily(found, turn_rows["by_day"]),
        "compactions": compactions(store.session_events(None, COMPACTION_EVENTS, since=since, repo=repo)),
        "primer": cells_mod.trial(store, repo),
        # Where it stands now: the verdict a brief would get in this repository (none across all).
        "primer_now": cells_mod.primer_arm(store, repo, 0)[1] if repo else None,
        # Workflow steps that fork the reading step before them, against fresh ones (`locks.trunks`).
        "trunk": trunks.trial(store, repo),
        "trunk_now": trunks.arm(store, repo, 0)[1] if repo else None,
        "role_revisions": revisions.overview(store, repo),
        # Whether workers handed livespec used it, and how their attempts went: observed, not a trial.
        "livespec_use": capability_use(store.capability_use("livespec", since=since, repo=repo)),
        # The tokens a run spent per kind, with livespec called against without (observed, not a trial).
        "livespec_tokens": livespec_tokens(found),
        "dossier": dossier_effect(found),
        # Whether racing a first try paid, per repository and kind (`locks.racing`): its own window.
        "races": racing.table(store, repo),
        # What each blind review of a two-review ledger added that no other saw (`workflows.reviewer_yield`).
        "reviewers": workflows.reviewer_yield(store, repo),
        # What the independent check's record says of each cell, and where its sampling stands (`locks.trust`).
        "trust": trust.report(store, repo),
        # Each capability workers were handed and whether they called it (observed), and the evidence trial.
        "capabilities": capabilities_used(events, {r["task"] for r in found}),
        # How workers worked, from their own tool calls, by kind and outcome and by cell (`locks.behavior`).
        "behavior": behavior.table(events, {r["task"]: r["kind"] for r in found}),
        # The reviews held back for reporting no finding and reading nothing (observed, never a verdict).
        "reviews_unread": reviews_unread(store, {r["task"] for r in found}, since=since),
        # The notes briefs carried and the ones workers said they relied on (observed, never a verdict).
        "notes_used": notes_relied_on(events, {r["task"] for r in found}),
        # Each route's advice and whether the session sent locks work after it (observed, issue #124).
        "routing": routing(store.routed_followed(since=since, repo=repo)),
        # The turns told their own reading was heavy, and whether locks work followed (observed).
        "heavy_reads": heavy_reads(store.routed_followed(since=since, repo=repo)),
        "recall": notes_mod.recall_trial(store, repo),
        "recall_now": notes_mod.recall_arm(store, repo, 0)[1] if repo else None,
        "evidence": evidence_mod.trial(store, repo),
        "evidence_now": evidence_mod.arm(store, repo, 0)[1] if repo else None,
        # Each method a kind's workers may be briefed to follow, and its trial (`locks.methods`).
        "methods": {m.name: methods_mod.trial(store, repo, m) for m in methods_mod.METHODS.values()},
        "methods_now": {m.name: methods_mod.arm(store, repo, m, 0)[1] for m in methods_mod.METHODS.values()}
        if repo else {},
        # Whether a plan's parts are read before they are built, and its trial (`locks.critique`).
        "critique": critique_mod.trial(store, repo),
        "critique_now": critique_mod.arm(store, repo, 0)[1] if repo else None,
        # The first tries of starts raised on a shape's record (`locks.predict`), observed.
        "predicted": predict_mod.report(store, repo),
        # Each climb of a ladder and whether the attempt it started passed (observed): a rung's record.
        "climbs": climbs_by_rung(store, {r["task"]: r["kind"] for r in found}, since=since),
        "expensive": expensive(found),
        # The parts of a fan-out that stopped on a point, per repository (observed).
        "parts_on_a_point": parts_on_a_point(found),
    }
    if keep_runs is None:
        report["runs"] = found
    else:
        report["slowest"] = sorted((r for r in found if r["wall_s"]), key=lambda r: -r["wall_s"])[:keep_runs]
        report["costliest"] = sorted(found, key=lambda r: -r["cost_usd"])[:keep_runs]
    return report


#: The moves that climb a ladder: the ones a rung is judged by.
CLIMBS = ("more_effort", "next_model")


def climbs_by_rung(store: Store, kinds: Mapping[int, str], *, since: str | None = None) -> list[dict[str, Any]]:
    """Each climb of the ladder over these tasks (`moved` events with a next cell), by kind and the
    cells it went between: how many, and how many of the attempts they started passed. Observed: the
    record a ladder's rung is judged by, never a rule's input. A climb whose next attempt never
    started (a cancel, a death) is left out."""
    out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    attempts: dict[int, list[dict]] = {}
    for event in store.events_of_kinds(["moved"], since=since):
        data, task_id = event["data"], int(event["task_id"])
        if task_id not in kinds or data.get("move") not in CLIMBS or not data.get("to_cell"):
            continue
        seq = int(data.get("seq") or 0)
        if task_id not in attempts:
            attempts[task_id] = store.attempts(task_id)
        after = [a for a in attempts[task_id] if int(a["seq"]) > seq]
        if not after:
            continue
        key = (kinds[task_id], str(data.get("from_cell")), str(data["to_cell"]), str(data["move"]))
        row = out.setdefault(key, {"kind": key[0], "from": key[1], "to": key[2], "move": key[3],
                                   "climbs": 0, "passed_next": 0})
        row["climbs"] += 1
        row["passed_next"] += int(bool(after[0]["passed"]))
    for row in out.values():
        row["passed_next_rate"] = _share(row["passed_next"], row["climbs"])
    return sorted(out.values(), key=lambda r: (r["kind"], -r["climbs"], r["from"], r["to"]))


#: A run costs much for its kind over this percentile of the kind's runs.
EXPENSIVE_Q = 0.9
#: Runs of a kind needed before its percentile says anything; below it, every run is weighed.
EXPENSIVE_MIN = 5


def expensive(found: Sequence[Mapping[str, Any]], *, top: int = 10) -> list[dict[str, Any]]:
    """The runs that cost the most for their kind (over its `EXPENSIVE_Q` cost, or the costliest
    when a kind has few runs), each with why, read from what it recorded: its turns and the
    moves that raised them, the models it climbed, a race, the independent check, the check's
    time, refusals. Money is measured, never a brake: this says where it went."""
    by_kind: dict[str, list[float]] = defaultdict(list)
    for r in found:
        by_kind[r["kind"] or "?"].append(r["cost_usd"] or 0.0)
    out = []
    for r in found:
        costs = by_kind[r["kind"] or "?"]
        line = pct(costs, EXPENSIVE_Q) if len(costs) >= EXPENSIVE_MIN else None
        if not r["cost_usd"] or (line is not None and r["cost_usd"] < line):
            continue
        why = []
        moves = Counter(m for m in r["moves"] if m)
        if moves.get("more_turns"):
            why.append(f"ran out of turns {moves['more_turns']}× and was given more ({r['turns']} turns in all)")
        elif r["turns"]:
            why.append(f"{r['turns']} turns")
        models = list(dict.fromkeys(str(c).split("/")[0] for c in r["cells"] if c))
        if len(models) > 1:
            why.append("climbed models: " + " → ".join(models))
        if moves.get("more_effort"):
            why.append(f"climbed effort {moves['more_effort']}×")
        if moves.get("race") or moves.get("raced_beside"):
            why.append("raced a second worker")
        if r["verifier_cost_usd"]:
            why.append(f"independent check {_usd(r['verifier_cost_usd'])}")
        if r["check_s"] and r["wall_s"] and r["check_s"] >= 0.25 * r["wall_s"]:
            why.append(f"its check took {_seconds(r['check_s'])} of {_seconds(r['wall_s'])}")
        if r["refused"]:
            why.append(f"{r['refused']} attempt(s) refused")
        if r["stop"]:
            why.append(f"stopped as {r['stop']}")
        out.append({"task": r["task"], "title": r.get("title"), "kind": r["kind"], "cost_usd": r["cost_usd"],
                    "kind_p90_usd": line, "attempts": r["attempts"], "why": why})
    return sorted(out, key=lambda x: -x["cost_usd"])[:top]


def expensive_text(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "No run cost more than most of its kind in this period."
    lines = ["The runs that cost the most for their kind, and why:"]
    for r in rows:
        bar = f" (p90 of {r['kind']}: {_usd(r['kind_p90_usd'])})" if r["kind_p90_usd"] is not None else ""
        lines.append(f"#{r['task']} {_usd(r['cost_usd'])}{bar}, {r['attempts']} attempt(s)"
                     + (f" — {r['title']}" if r.get("title") else ""))
        lines += [f"  · {w}" for w in r["why"]] or ["  · nothing it recorded stands out"]
    return "\n".join(lines)


def attempts(tasks: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], *,
             since: str = "") -> list[dict[str, Any]]:
    """One record per attempt that finished since `since`, oldest first: its task, repository and
    kind, its cell, how it ended, its time, tokens and cost, and its worker's profile flattened
    (`behavior.FIELDS`, `tools` by family). For an analysis outside locks; read only."""
    by_id = {int(t["id"]): t for t in tasks}
    out = []
    for e in events:
        task = by_id.get(int(e["task_id"]))
        if e["kind"] != "attempt_finished" or task is None or (since and (e.get("ts") or "") < since):
            continue
        data = e.get("data") or {}
        worked = data.get("behavior") or {}
        out.append({
            "task": task["id"], "seq": data.get("seq"), "repo": task.get("repo"), "kind": task.get("kind"),
            "ts": e.get("ts"), "refused": bool(data.get("denied")),
            **{k: data.get(k) for k in ATTEMPT_COLUMNS[5:19]},
            **{k: worked.get(k) for k in behavior.FIELDS}, "tools": worked.get("tools") or {},
        })
    return out


def attempts_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """One row per attempt, for a spreadsheet: `tools` as `family:calls` pairs."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(ATTEMPT_COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in rows:
        flat = {k: ("" if r.get(k) is None else r.get(k)) for k in ATTEMPT_COLUMNS}
        flat["tools"] = " ".join(f"{k}:{v}" for k, v in (r.get("tools") or {}).items())
        writer.writerow(flat)
    return out.getvalue()


def runs_csv(found: Sequence[Mapping[str, Any]]) -> str:
    """One row per run, for a spreadsheet."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(RUN_COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in found:
        writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in RUN_COLUMNS})
    return out.getvalue()


def _seconds(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}s" if value < 120 else f"{value / 60:.1f}m" if value < 7200 else f"{value / 3600:.1f}h"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _tokens(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 1000:.0f}k" if value >= 1000 else f"{value:.0f}"


def _tokens_line(row: Mapping[str, Any]) -> str:
    arms = "  ".join(f"{arm} {row[arm]['runs']} run(s) avg {_tokens(row[arm]['avg'])} p50 {_tokens(row[arm]['p50'])}"
                     for arm in ("with", "without"))
    return f"  {row['kind']:<16} {arms}" + (f"  ({row['change']:+.0%})" if row["change"] is not None else "")


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.2f}"


def text(report: Mapping[str, Any]) -> str:
    s = report["summary"]
    scope = report["repo"] or "every repository"
    period = f"last {report['days']} days" if report["days"] else "all time"
    lines = [f"locks stats — {scope}, {period}", ""]
    if not s["runs"]:
        lines.append("No run of locks' yet in this period.")
    else:
        lines += [
            f"runs {s['runs']} ({s['tasks']} tasks)  passed {s['passed']} ({_rate(s['pass_rate'])})  first try "
            f"{_rate(s['first_try_rate'])}  climbed {_rate(s['climbed_rate'])}  verified {_rate(s['verified_rate'])}",
            f"time p50 {_seconds(s['wall_p50_s'])}  p90 {_seconds(s['wall_p90_s'])}  queue p50 "
            f"{_seconds(s['queued_p50_s'])}  {s['hours']}h in all, {s['minutes_per_pass'] or '—'} min per pass",
            f"cost {_usd(s['cost_usd'])}  per pass {_usd(s['cost_per_pass_usd'])}  p50 {_usd(s['cost_p50_usd'])}"
            f"  independent checks {_usd(s['verifier_cost_usd'])}  cache read {_rate(s['cache_read_share'])}",
            "",
        ]
        ph = report["phases"]
        if ph["runs"]:
            lines.append(f"where the time went ({ph['runs']} timed runs): " + "  ".join(
                f"{k[:-2]} {_seconds(ph['seconds'][k])} ({_rate(ph['share'][k])})" for k in PHASES
                if ph["seconds"][k]))
            told = report.get("pickup") or {}
            if any((told.get("seconds") or {}).values()):
                lines.append("  the pickup waited on: " + "  ".join(
                    f"{PICKUP_WORDS[k]} {_seconds(v)} ({_rate(told['share'][k])})"
                    for k, v in told["seconds"].items() if v)
                    + (f"  (not told: {_seconds(told['unknown_s'])})" if told.get("unknown_s") else ""))
            lines.append("")
        use = report.get("livespec_use") or {}
        if use.get("given"):
            lines.append(f"livespec: handed to {use['given']} attempt(s), called in {use['used']} ({use['calls']} "
                         f"call(s)); passed {_rate(use['passed_used'])} when called, {_rate(use['passed_idle'])} "
                         "when not (observed, not a trial)")
            lines.append("")
        dossier = report.get("dossier") or {}
        if (dossier.get("with") or {}).get("runs"):
            lines.append("briefs that carried livespec's dossier against those that did not (observed, not a trial):")
            for side, row in dossier.items():
                took = f"{row['pass_p50_s']:.0f}s" if row.get("pass_p50_s") is not None else "-"
                lines.append(f"  {side:<8} runs {row['runs']:>4}  first tries {row['passed']}/{row['tries']} "
                             f"({_rate(row['rate'])})  pass p50 {took}")
            lines.append("")
        if rows := report.get("livespec_tokens"):
            lines.append("tokens per run by kind, livespec called against not called (observed, not a trial):")
            lines += [_tokens_line(r) for r in rows]
            lines.append("")
        lines.append("by kind:")
        for row in report["by_kind"]:
            lines.append(f"  {row['kind']:<16} runs {row['runs']:>4}  pass {_rate(row['pass_rate']):>4}  first "
                         f"{_rate(row['first_try_rate']):>4}  p50 {_seconds(row['wall_p50_s']):>6}  per pass "
                         f"{_usd(row['cost_per_pass_usd'])}")
        if report["context"]:
            lines += ["", "the session's thread in the brief (runs a session sent):"]
            for row in report["context"]:
                lines.append(f"  {row['group']:<26} runs {row['runs']:>4}  pass {_rate(row['pass_rate']):>4}  first "
                             f"{_rate(row['first_try_rate']):>4}  attempts {row['attempts_avg']}")
        if report.get("continuations"):
            lines += ["", "after more effort or more turns, the attempt went on in the last one's session or "
                          "started fresh:"]
            for row in report["continuations"]:
                lines.append(f"  {row['move']:<12} {'continued' if row['continued'] else 'fresh':<9} attempts "
                             f"{row['attempts']:>4}  passed {row['passed']:>4}  turns p50 {row['turns_p50'] or '—'}"
                             f"  cost p50 {_usd(row['cost_p50_usd'])}")
        primer = report.get("primer") or {}
        if any(row["tasks"] for row in primer.values()):
            lines += ["", "the cell's primer in the brief (its trial; first tries, calls before the first change):"]
            for arm, row in primer.items():
                rate = row["passed_first"] / row["tasks"] if row["tasks"] else None
                lines.append(f"  {'with it' if arm == 'primer' else 'without it':<11} tasks {row['tasks']:>4}  first "
                             f"{_rate(rate):>4}  calls before the first change p50 "
                             f"{row['calls_before_edit_p50'] if row['calls_before_edit_p50'] is not None else '—'}"
                             f"  input p50 {row['input_tokens_p50'] or '—'}")
            if report.get("primer_now"):
                lines.append(f"  now: {report['primer_now']}")
        trunk = report.get("trunk") or {}
        if any(row["tasks"] for row in trunk.values()):
            lines += ["", "workflow steps that fork the step that read for them (the trunk's trial; first tries):"]
            for arm, row in trunk.items():
                rate = row["passed_first"] / row["tasks"] if row["tasks"] else None
                calls = row["calls_before_edit_p50"]
                took = row["duration_p50_s"]
                lines.append(f"  {arm:<11} tasks {row['tasks']:>4}  first {_rate(rate):>4}  calls before the first "
                             f"change p50 {calls if calls is not None else '—'}  input p50 "
                             f"{row['input_tokens_p50'] or '—'}  time p50 {f'{took}s' if took is not None else '—'}")
            if report.get("trunk_now"):
                lines.append(f"  now: {report['trunk_now']}")
        if report.get("role_revisions"):
            lines += ["", "the roles' lessons here (a revision on trial, its arms' first tries, and its history):"]
            lines += ["  " + line for line in revisions.text(report["role_revisions"], lessons=False)]
        if report.get("races"):
            lines += ["", "first tries that raced the next rung (each repository's last tasks of the kind):"]
            for row in report["races"]:
                lines.append(f"  {_name(row['repo']):<16} {row['kind']:<12} races {row['races']:>3}  rung won "
                             f"{row['rung_wins']:>3}  start won {row['start_wins']:>3}  both failed "
                             f"{row['neither']:>3}  {'racing' if row['pays'] else 'not racing'}: {row['why']}")
        if report.get("reviewers"):
            lines += ["", "blind reviews beside another (rows each put on the ledger, rows no other saw, and of those "
                      "the ones kept):"]
            for row in report["reviewers"]:
                lines.append(f"  {row['workflow']:<14} {row['step']:<12} {row['cell']:<12} runs {row['runs']:>3}  rows "
                             f"{row['rows']:>4}  alone {row['alone']:>3}  kept {row['alone_kept']:>3}"
                             + ("  added nothing the others missed" if row["runs"] >= 3 and not row["alone_kept"]
                                else ""))
        if report.get("capabilities"):
            lines += ["", "capabilities handed to workers (attempts that had each, called it, had a call go through; "
                      "attempts passed when called against when not, observed):"]
            for row in report["capabilities"]:
                lines.append(f"  {row['capability']:<18} handed {row['handed']:>4}  called {row['called']:>4} "
                             f"({_rate(row['called_rate'])})  through {row['through']:>4}  never called "
                             f"{row['never_called']:>4}  passed called {_rate(row['pass_when_called'])} / silent "
                             f"{_rate(row['pass_when_silent'])}"
                             + (f"  ({row['unlogged']} attempt(s) not recorded: their worker's hooks logged no call)"
                                if row.get("unlogged") else ""))
        lines += behavior_lines(report.get("behavior") or {})
        lines += unread_lines(report.get("reviews_unread") or {})
        for mode, row in (report.get("routing") or {}).items():
            lines.append(f"route {mode}: advised {row['advised']}, the session sent locks work after "
                         f"{row['sent']} ({_rate(row['sent_rate'])}) (observed)")
        if reads := report.get("heavy_reads"):
            lines.append(f"heavy reads said {reads['advised']} time(s) (the session read "
                         f"{route_mod.READ_HEAVY_CHARS:,} characters or more of tool output in a turn), the session "
                         f"sent locks work after {reads['sent']} ({_rate(reads['sent_rate'])}) (observed)")
        for row in report.get("parts_on_a_point") or ():
            where = f" ({_name(row['repo'])})" if not report.get("repo") else ""
            lines.append(f"parts stopped on a point: {row['stopped']} of {row['parts']}{where} (observed)")
        if relied := report.get("notes_used"):
            lines += ["", f"notes in briefs: {relied['tasks']} task(s) carried {relied['carried']}, workers said they "
                      f"relied on {relied['relied_on']} ({_rate(relied['relied_rate'])}); first tries passed "
                      f"{_rate(relied['pass_first_naming'])} where a worker named one, "
                      f"{_rate(relied['pass_first_silent'])} where none did (observed)"
                      + (f"; {relied['tasks_unsaid']} task(s) whose workers did not say" if relied.get("tasks_unsaid")
                         else "")]
        order = report.get("recall") or {}
        if any(side.get("tasks") for side in order.values()):
            lines += ["", "the order of a brief's notes (its trial; tasks, first tries passed):"]
            for side, row in order.items():
                lines.append(f"  {side:<8} tasks {row['tasks']:>4}  first tries {row['passed']}/{row['tries']} "
                             f"({_rate(row['rate'])})")
            if report.get("recall_now"):
                lines.append(f"  now: {report['recall_now']}")
        held = report.get("evidence") or {}
        if any(arm.get("tasks") for arm in held.values()):
            lines += ["", "holding work to evidence (its trial; tasks, read by the independent check, blocked):"]
            for arm, row in held.items():
                lines.append(f"  {arm:<6} tasks {row['tasks']:>4}  read {row['read']:>4}  blocked {row['blocked']:>4} "
                             f"({_rate(row['blocked_rate'])})")
            if report.get("evidence_now"):
                lines.append(f"  now: {report['evidence_now']}")
        for name, arms in (report.get("methods") or {}).items():
            if any(side.get("tasks") for side in arms.values()):
                lines += ["", f"the {name} method (its trial; tasks, first tries passed):"]
                for side, row in arms.items():
                    lines.append(f"  {side:<8} tasks {row['tasks']:>4}  first tries {row['passed']}/{row['tries']} "
                                 f"({_rate(row['rate'])})")
                if (report.get("methods_now") or {}).get(name):
                    lines.append(f"  now: {report['methods_now'][name]}")
        read = report.get("critique") or {}
        if any(side.get("tasks") for side in read.values()):
            lines += ["", "the critique of a plan's parts (its trial; plans, first tries of the parts after them "
                      "passed):"]
            for side, row in read.items():
                if row.get("tasks"):
                    lines.append(f"  {side:<8} plans {row['tasks']:>4}  first tries {row['passed']}/{row['tries']} "
                                 f"({_rate(row['rate'])})")
            if report.get("critique_now"):
                lines.append(f"  now: {report['critique_now']}")
        if report.get("predicted"):
            lines += ["", "starts raised on the record of the task's shape (`predict`; its first tries, observed):"]
            for row in report["predicted"]:
                s = row["shape"]
                rate = _rate(_share(row["passed_first"], row["predicted"]))
                lines.append(f"  {_name(row['repo']):<16} {row['kind']:<12} {s['points']} points, {s['tier']} risk, "
                             f"{s['areas']} area(s)  from {row['start']:<13} predicted {row['predicted']:>3}  first "
                             f"tries passed {row['passed_first']} ({rate})")
        if report.get("climbs"):
            lines += ["", "climbs by rung (observed: each climb of a ladder, and whether the attempt it started "
                      "passed):"]
            for row in report["climbs"]:
                move = row["move"].replace("_", " ")
                lines.append(f"  {row['kind']:<16} {row['from']:>13} → {row['to']:<13} {move:<12} "
                             f"climbs {row['climbs']:>3}  passed next {row['passed_next']} "
                             f"({_rate(row['passed_next_rate'])})")
        if report.get("trust"):
            lines += ["", "the independent check's record by cell (its sampling, repeated blockers, re-checks finding "
                      "something new, and the work that started there):"]
            lines += ["  " + line for line in trust.text(report["trust"])]
        if report["stops"]:
            lines += ["", "stopped by: " + ", ".join(f"{k} {v}" for k, v in report["stops"].items())]
    t = report["turns"]["turns"]
    if t["count"]:
        lines += ["", f"session turns {t['count']}  p50 {_seconds(t['seconds_p50'])}  p90 {_seconds(t['seconds_p90'])}"
                  f"  ${t['usd']:.2f}  interrupted {t['interrupted']}"]
    lines += _compaction_lines(report.get("compactions") or [])
    return "\n".join(lines)


def behavior_lines(table: Mapping[str, Any]) -> list[str]:
    """`locks stats`' lines of how workers worked, by kind and outcome (medians; shares)."""
    rows = table.get("by_kind") or []
    if not rows:
        return []

    def num(value: float | None) -> str:
        return "—" if value is None else f"{value:g}"

    lines = ["", "how workers worked, from their tool calls (attempts; medians of calls, calls before the first "
             "write, checks run, fix loops, rereads; shares of failed calls, writes left unchecked, attempts "
             "that never checked; observed):"]
    for r in rows:
        lines.append(f"  {r['kind'] + ' ' + r['outcome']:<22} {r['attempts']:>4}  calls {num(r['calls_p50'])}  "
                     f"before write {num(r['calls_before_edit_p50'])}  checks {num(r['check_runs_p50'])}  "
                     f"loops {num(r['fix_cycles_p50'])}  rereads {num(r['rereads_p50'])}  failed "
                     f"{_rate(r['failed_share'])}  unchecked {_rate(r['unchecked_end_share'])}  never checked "
                     f"{_rate(r['never_checked_share'])}")
    return lines


def _name(repo: str | None) -> str:
    return str(repo or "-").rstrip("/").rsplit("/", 1)[-1]


def _compaction_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rows:
        return []
    said = [r for r in rows if r["now"]]
    # Advice the orchestrating session heard and chose not to take is its call, not a failure.
    declined = sum(1 for r in said if not r["ran"] and r["delivered"] == "advise")
    lines = ["", f"compaction decisions {len(rows)}  said now {len(said)}  delivered "
             f"{sum(1 for r in said if r['delivered'])}  ran {sum(1 for r in said if r['ran'])}"
             + (f"  advice the session chose not to take {declined}" if declined else "")
             + (f"  not run otherwise {n}" if (n := sum(1 for r in said if not r["ran"]) - declined) else "")]
    for r in rows[-COMPACTIONS_SHOWN:]:
        price = (f"warm {_usd(r['warm_usd'])} vs cold {_usd(r['cold_usd'])} ({_rate(r['warm_share'])})"
                 if r["cold_usd"] else "cost not measurable")
        if r["now"]:
            outcome = (f"{r['delivered'] or 'not delivered'}, "
                       + ("ran" if r["ran"] else "did not run" if r["ran"] is False else "not seen to run"))
        else:
            outcome = next((b for b in r["because"] if b.startswith("not now: ")), "not now")
        lines.append(f"  {str(r['at'])[:16]} #{r['decision']} {r['tokens'] // 1000}k ({_rate(r['share'])})  "
                     f"{'now' if r['now'] else 'wait'}  {price}  {outcome}")
    return lines


def as_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
