"""How locks evolves, and why: a snapshot a day of what its work measured, read
beside every change of a standard (`locks evolution`, the UI's Evolution).

Once a day — at the first ending of a new day in the person's time zone, from the
hooks or the dispatcher (`settle`), or when a person asks (`locks evolution
--snapshot`) — one row per repository and kind is appended for the tasks that
ended since the last snapshot: tasks and points done, velocity (points per hour of
wall time), what a point cost and took, how often the first attempt passed, how
often a task climbed, the points estimated beside the points realized, and
locks' version. The report reads a window of them: each metric's trend (the
older half of the snapshots against the newer), and every change of a standard
(`locks.standards`) with its rule, its evidence and its verdict, in plain
sentences. No model reads or writes any of it; `settle` and `snapshot` are the
only writers, and a GET is neither.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from locks import __version__, standards
from locks.store import Store

#: The day the last snapshot was taken, in the person's time zone, and where it ended.
SNAPSHOT_KEY = "evolution:snapshot"
#: Each metric of a trend: its key, how it reads, and which way is better (+1 up, -1 down).
METRICS = (("velocity", "velocity", 1), ("cost_per_point", "cost per point", -1),
           ("time_per_point_s", "time per point", -1), ("pass_first", "pass-first", 1), ("climbed", "climbs", -1))


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _today(now: datetime, tz: tzinfo | None) -> str:
    return now.astimezone(tz).date().isoformat()


def _row(tasks: Sequence[standards.Ended]) -> dict[str, Any]:
    """What a day's tasks of one repository and kind measured."""
    sized = [t for t in tasks if t.band]
    done = sum(t.band or 0 for t in sized if t.passed)
    both = [t for t in sized if t.realized]
    hours = sum(t.wall_s for t in sized) / 3600
    # Velocity is over the tasks whose time was measured: an unmeasured one adds points and no hours.
    timed = [t for t in sized if t.wall_s > 0]
    timed_hours = sum(t.wall_s for t in timed) / 3600
    timed_done = sum(t.band or 0 for t in timed if t.passed)
    measured = standards.metrics(tasks)
    return {"tasks": len(tasks), "done": sum(t.passed for t in tasks), "points_done": done,
            "points_estimated": sum(t.band or 0 for t in both), "points_realized": sum(t.realized or 0 for t in both),
            "hours": round(hours, 3), "cost_usd": round(sum(t.cost_usd for t in tasks), 4),
            "velocity": round(timed_done / timed_hours, 2) if timed_hours else None,
            "cost_per_point": measured["cost_per_point"], "time_per_point_s": measured["time_per_point_s"],
            "pass_first": measured["pass_first"],
            "climbed": round(sum(len(t.attempts) > 1 for t in tasks) / len(tasks), 3) if tasks else None}


def _take(store: Store, now: datetime, tz: tzinfo | None, until_now: bool) -> list[dict[str, Any]]:
    today = _today(now, tz)
    local = now.astimezone(tz)
    until = _stamp(now if until_now else datetime.combine(local.date(), time(0), tzinfo=local.tzinfo))
    since = str(json.loads(store.meta(SNAPSHOT_KEY) or "{}").get("until") or "")
    rows = []
    if until > since:
        groups: dict[tuple[str, str], list[standards.Ended]] = defaultdict(list)
        for task in standards.ended(store, since=since, until=until):
            if task.repo and task.kind:
                groups[(task.repo, task.kind)].append(task)
        for (repo, kind), tasks in sorted(groups.items()):
            row = {"at": _stamp(now), "day": today, "since": since, "until": until, "repo": repo, "kind": kind,
                   **_row(tasks), "version": __version__}
            store.add_snapshot(**row)
            rows.append(row)
    store.set_meta(SNAPSHOT_KEY, json.dumps({"day": today, "until": max(until, since)}))
    return rows


def snapshot(store: Store, *, now: datetime | None = None, tz: tzinfo | None = None,
             until_now: bool = False) -> list[dict[str, Any]]:
    """Append a snapshot of what ended since the last one: up to the start of today in the
    person's time zone (`tz`; None is this machine's), or up to now with `until_now` (a
    person's `locks evolution --snapshot`). The rows written."""
    with store.immediate():
        return _take(store, now or _now(), tz, until_now)


def settle(store: Store, *, now: datetime | None = None, tz: tzinfo | None = None) -> dict[str, Any]:
    """After work ended: the standards settle (`standards.settle`: verdicts, and a calibration
    when one is due), and the first ending of a new day takes the day's snapshot. From the
    hooks and the dispatcher, never from a GET. `snapshot` is None when none was due; `notes`
    says which candidate notes were made current or dropped (`notes.weigh`)."""
    now = now or _now()
    from locks import cells, notes

    cells.backfill(store)
    found = standards.settle(store, now=now)
    from locks import revisions

    if revisions.enabled():
        # A role's lessons here: the verdicts due, and a revision proposed when its evidence is.
        found = {**found, "roles": revisions.settle(store, now=now)}
    # A worker-learned note proven or dropped by the tasks whose brief carried it.
    weighed = notes.weigh(store)
    # A current note every brief carries and no worker names goes back on trial (`notes.retry_idle`).
    if retried := notes.retry_idle(store):
        weighed = {**weighed, "retried": retried}
    from locks import habits
    from locks.store import home

    # A habits review's proposals that pass locks' own check are installed (`habits.apply_reviews`).
    installed = habits.apply_reviews(store, home())
    taken = None
    if found["ended"]:
        with store.immediate():
            if json.loads(store.meta(SNAPSHOT_KEY) or "{}").get("day") != _today(now, tz):
                taken = _take(store, now, tz, False)
    return {**found, "snapshot": taken, "notes": weighed, "habits": installed}


# --- the report --------------------------------------------------------------------------------

def _name(repo: str | None) -> str:
    return str(repo or "—").rstrip("/").rsplit("/", 1)[-1]


def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f} %"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f}m" if seconds < 7200 else \
        f"{seconds / 3600:.1f}h"


def _shown(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    if key == "velocity":
        return f"{value:.1f}"
    if key == "cost_per_point":
        return f"${value:.2f}"
    if key == "time_per_point_s":
        return _duration(value)
    return _pct(value)


def _together(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Several snapshot rows as one: counts added, each rate weighed by what it was over."""
    def weighed(key: str, weight: str) -> float | None:
        pairs = [(r[key], r[weight]) for r in rows if r[key] is not None and r[weight]]
        total = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / total, 4) if total else None

    done, hours = sum(r["points_done"] for r in rows), sum(r["hours"] for r in rows)
    return {"velocity": round(done / hours, 2) if hours else None,
            "cost_per_point": weighed("cost_per_point", "points_done"),
            "time_per_point_s": weighed("time_per_point_s", "points_done"),
            "pass_first": weighed("pass_first", "tasks"), "climbed": weighed("climbed", "tasks")}


def _said(before: float | None, after: float | None, better: int, alone: bool) -> str:
    if alone:
        return "one snapshot"
    if before is None or after is None:
        return "not measured"
    if round(after, 4) == round(before, 4):
        return "same"
    return "better" if (after - before) * better > 0 else "worse"


def trends(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per repository and kind, each metric from the older half of its snapshots to the newer."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["repo"], row["kind"])].append(row)
    out = []
    for (repo, kind), found in sorted(groups.items()):
        found = sorted(found, key=lambda r: (r["day"], r["id"]))
        alone = len(found) == 1
        older, newer = (found, found) if alone else (found[:len(found) // 2], found[len(found) // 2:])
        a, b = _together(older), _together(newer)
        metrics = {key: {"from": a[key], "to": b[key], "said": _said(a[key], b[key], better, alone)}
                   for key, _, better in METRICS}
        totals = {k: sum(r[k] for r in found) for k in ("tasks", "done", "points_done", "points_estimated",
                                                         "points_realized")}
        said = ", ".join(
            f"{label} {_shown(key, metrics[key]['to'])}" + (" points/h" if key == "velocity" else "") if alone else
            f"{label} {_shown(key, metrics[key]['from'])} → {_shown(key, metrics[key]['to'])}"
            + (" points/h" if key == "velocity" else "") + f" ({metrics[key]['said']})"
            for key, label, _ in METRICS)
        line = (f"{kind} in {_name(repo)}: {said}; {totals['points_estimated']} points estimated, "
                f"{totals['points_realized']} realized; {totals['points_done']} points done in {totals['tasks']} "
                f"tasks over {len(found)} snapshot{'s' if len(found) != 1 else ''}")
        out.append({"repo": repo, "kind": kind, "snapshots": len(found), **totals, "metrics": metrics,
                    "series": [dict(r) for r in found], "line": line})
    return out


def _what(field: str, value: str | None) -> str:
    if value is None:
        return "locks' rules"
    return value if field == "start_cell" else f"{value} turns"


def _skip_line(change: Mapping[str, Any], verdict: Mapping[str, Any] | None, name: str, when: str,
               tz: tzinfo | None) -> str:
    """A skip of a rung as a sentence: which climb, onto where, because of what, and its verdict."""
    if change["reverts"]:
        source, _, target = (change["old_value"] or "").partition(">")
        return f"{name}: climbs from {source} go to {target} again since {when}: {change['rule']}"
    evidence = json.loads(change["evidence"]) if isinstance(change["evidence"], str) else change["evidence"]
    head = (f"{name}: climbs from {evidence.get('from')} skip {evidence.get('to')} for {evidence.get('onto')} "
            f"since {when} because {standards.because(change)}")
    if verdict is None:
        return f"{head}; judged after {standards.JUDGE_AFTER} climbs past it"
    after = json.loads(verdict["window_after"])
    how = "kept" if verdict["verdict"] == "kept" else f"reverted on {standards.day(verdict['at'], tz)}"
    return f"{head}; the climbs since passed {_pct(after.get('pass_first'))}, {how}"


def change_line(change: Mapping[str, Any], verdict: Mapping[str, Any] | None, under: Sequence[standards.Ended],
                tz: tzinfo | None = None) -> str:
    """A change of a standard as a sentence: what it set, since when, because of what, and how
    the tasks since went — "feature in mecha, 5 points: starts at opus/high since Oct 20 because 2
    of 8 passed first at sonnet/xhigh and 4 of 5 at opus/high; pass-first since then 80 %, kept"."""
    name = f"{change['kind']} in {_name(change['repo'])}, {standards.size(change['band'])}"
    when = standards.day(change["at"], tz)
    if change["field"] == standards.SKIP:
        return _skip_line(change, verdict, name, when, tz)
    if change["reverts"]:
        return f"{name}: back to {_what(change['field'], change['new_value'])} since {when}: {change['rule']}"
    value = _what(change["field"], change["new_value"])
    what = (f"starts at {value}" if change["field"] == "start_cell" and change["new_value"]
            else f"starts with {value}" if change["new_value"] else f"starts by {value}")
    head = f"{name}: {what} since {when} because {standards.because(change)}"
    if verdict is not None:
        after = json.loads(verdict["window_after"])
        if verdict["verdict"] == "carried":
            return f"{head}; {verdict['reason']}"
        how = "kept" if verdict["verdict"] == "kept" else f"reverted on {standards.day(verdict['at'], tz)}"
        return f"{head}; pass-first since then {_pct(after.get('pass_first'))}, {how}"
    if under:
        rate = standards.metrics(under)["pass_first"]
        return (f"{head}; pass-first since then {_pct(rate)} over {len(under)} task{'s' if len(under) != 1 else ''}, "
                f"judged after {standards.JUDGE_AFTER}")
    return f"{head}; no task has run under it yet"


def _standard_line(row: Mapping[str, Any]) -> str:
    measured = row["measured"]
    cell = row["start_cell"] or "locks' rules"
    turns = f"{row['start_turns']} turns" if row["start_turns"] else "the kind's turns"
    return (f"{row['kind']} in {_name(row['repo'])}, {standards.size(row['band'])}: starts at {cell} with {turns}; "
            f"over {measured.get('tasks', 0)} tasks pass-first {_pct(measured.get('pass_first'))}, "
            f"{_shown('cost_per_point', measured.get('cost_per_point'))} and "
            f"{_duration(measured.get('time_per_point_s'))} per point")


def report(store: Store, *, days: int = 30, repo: str | None = None, now: datetime | None = None,
           tz: tzinfo | None = None) -> dict[str, Any]:
    """The evolution over the last `days` (0: all) of one repository (None: every one): the
    trends, every change with its verdict, and the standards as they are. Read only."""
    now = now or _now()
    since = now - timedelta(days=days) if days else None
    rows = store.snapshots(since_day=since.astimezone(tz).date().isoformat() if since else "", repo=repo)
    verdicts = store.verdicts()
    found = store.calibrations(repo=repo, since=_stamp(since) if since else "")
    ended = standards.ended(store, repo=repo) if found else []
    changes = []
    for change in found:
        under = [t for t in ended if change["id"] in t.used and t.repo == change["repo"]]
        verdict = verdicts.get(change["id"])
        changes.append({**change, "evidence": json.loads(change["evidence"]),
                        "verdict": {**verdict, "window_before": json.loads(verdict["window_before"]),
                                    "window_after": json.loads(verdict["window_after"])} if verdict else None,
                        "under": len(under), "line": change_line(change, verdict, under, tz)})
    kept = [{**row, "measured": json.loads(row["measured"] or "{}")} for row in store.standards(repo=repo)]
    return {"days": days, "repo": repo, "generated_at": _stamp(now), "trends": trends(rows), "changes": changes,
            "learned": learned(store, repo=repo, since=_stamp(since) if since else ""),
            "standards": [{**row, "line": _standard_line(row)} for row in kept]}


#: A trial's reason that says it was decided: a verdict of its own arms, or the pooled one.
_DECIDED = re.compile(r"\b(?:won|lost) its trial\b")
#: The reason a resume's brief keeps beside its arm: it says nothing of where the trial stands.
_KEPT_ARM = "the arm this task's first brief drew"


def learned(store: Store, *, repo: str | None = None, since: str = "") -> dict[str, list[dict[str, Any]]]:
    """What the record decided beyond the standards, read and never written: where each trial of a
    repository stands (the last reason a brief there kept beside its arm), a role's lessons proposed,
    adopted, dropped or reverted since `since`, the notes the trial moved since then with how many
    still wait, and the habits installed with what they caught. Every line is a record locks already
    kept; nothing here is computed again, so it says what the briefs were told."""
    trials: dict[tuple[str, str], dict[str, Any]] = {}
    for event in store.session_events(None, ["briefed"], repo=repo):
        for key, why in event["data"].items():
            name = key.removesuffix("_why")
            arm = event["data"].get(f"{name}_arm")
            if name == key or not why or not arm or why == _KEPT_ARM:
                continue
            trials[(event["repo"] or "", name)] = {
                "repo": event["repo"], "trial": name, "arm": arm, "why": why, "at": event["ts"],
                "task": event["task_id"], "decided": bool(_DECIDED.search(str(why)))}
    roles = [{"repo": r["repo"], "role": r["role"], "record": r["record"], "reason": r["reason"], "at": r["at"],
              "lessons": len(r["lessons"])} for r in store.role_revisions(repo=repo, since=since)]
    notes: list[dict[str, Any]] = []
    from locks import notes as notes_mod

    recalled = {i for held in (notes_mod.carried(store), notes_mod.withheld(store)) for ids in held.values()
                for i in ids}
    for project in store.note_projects():
        if repo is not None and project != repo:
            continue
        found = store.notes(project, limit=100_000)
        moved = [n for n in found if n["updated_at"] >= since and n["state"] in ("current", "dropped")
                 and str(n.get("state_reason") or "").startswith(("proven", "disproven", "unproven"))]
        waiting = sum(1 for n in found if n["state"] == "candidate")
        if moved or waiting:
            notes.append({"repo": project, "waiting": waiting,
                          "unrecalled": sum(1 for n in found if n["state"] == "candidate"
                                            and n["id"] not in recalled),
                          "adopted": sum(1 for n in moved if n["state"] == "current"),
                          "dropped": [{"id": n["id"], "title": n["title"], "reason": n["state_reason"]}
                                      for n in moved if n["state"] == "dropped"]})
    habits = [{"id": h["id"], "steps": json.loads(h["steps"]), "command": h["command"], "runs": h["runs"],
               "caught": h.get("caught") or 0, "off": bool(h["disabled_at"]),
               "where": str(Path(h["settings_path"]).parent.parent)} for h in store.habits()]
    return {"trials": sorted(trials.values(), key=lambda t: (t["repo"] or "", t["trial"])), "roles": roles,
            "notes": notes, "habits": habits}


def learned_lines(found: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    lines = []
    for t in found["trials"]:
        state = "decided" if t["decided"] else "still trying"
        lines.append(f"{t['trial']} in {_name(t['repo'])}: {state}, last arm {t['arm']} (#{t['task']}): {t['why']}")
    for r in found["roles"]:
        lines.append(f"role {r['role']} in {_name(r['repo'])}: {r['record']} {r['lessons']} lessons: {r['reason']}")
    for n in found["notes"]:
        lines.append(f"notes in {_name(n['repo'])}: {n['adopted']} adopted and {len(n['dropped'])} dropped by "
                     f"their trial in this period, {n['waiting']} still candidates"
                     + (f" ({n['unrecalled']} of them no brief recalled yet)" if n.get("unrecalled") else ""))
        lines += [f"  dropped #{d['id']} {d['title']}: {d['reason']}" for d in n["dropped"]]
    for h in found["habits"]:
        lines.append(f"habit #{h['id']} {' → '.join(h['steps'])} in {h['where']}: {h['runs']} runs, "
                     f"{h['caught']} found something" + (", turned itself off" if h["off"] else ""))
    return lines


def text(found: Mapping[str, Any]) -> str:
    scope = found["repo"] or "every repository"
    period = f"last {found['days']} days" if found["days"] else "all time"
    lines = [f"locks evolution — {scope}, {period}", ""]
    if found["trends"]:
        lines.append("Trends, from the older half of the snapshots to the newer:")
        lines += [f"  {t['line']}" for t in found["trends"]]
    else:
        where = f"in the {period}" if found["days"] else "at all"
        lines.append(f"No snapshot {where} yet: the first task to end on a new day takes one, or "
                     "`locks evolution --snapshot` takes one now.")
    lines.append("")
    if found["changes"]:
        lines.append("Calibration changes, oldest first:")
        lines += [f"  #{c['id']} {c['line']}" for c in found["changes"]]
    else:
        lines.append("No standard changed in this period.")
    said = learned_lines(found.get("learned") or {"trials": [], "roles": [], "notes": [], "habits": []})
    if said:
        lines += ["", "Decided by the record, beyond the standards:"] + [f"  {line}" for line in said]
    if found["standards"]:
        lines += ["", "Standards now:"] + [f"  {s['line']}" for s in found["standards"]]
    return "\n".join(lines)


def as_json(found: Mapping[str, Any]) -> str:
    return json.dumps(found, ensure_ascii=False, indent=2, default=str)
