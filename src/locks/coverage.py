"""Which of locks' features this machine's records show ever ran (`locks coverage`).

Read only, no model: every figure is a count over a copy of the store (`forensics.open_copy`),
by the event kind, event field, table or task option each feature leaves behind. A feature
whose record cannot be named from what the code really writes is left out, never guessed:
`tests/test_coverage.py` checks that every event kind named here is written in `src/locks`.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks import matrix

AREAS = ("routing", "checks", "trials", "workflows", "memory", "habits", "evolution", "session", "neighbours")

#: The arms of a brief, each a field of the `briefed` event: a trial ran when the field is there.
BRIEFED_ARMS = ("primer_arm", "trunk_arm", "critique_arm", "dossier_arm", "evidence_arm", "method_arm",
                "recall_arm", "revision_arm")


@dataclass(frozen=True, slots=True)
class Feature:
    """One feature, and how its use is read: `how` is `events` (kinds), `field` (event, key),
    `moves` (attempts whose move is one of), `table` (name, time column, repo column),
    `kind` (tasks of a kind), `option` (tasks whose options hold the key) or `workflow` (runs
    of a template)."""

    name: str
    area: str
    line: str
    how: str
    source: tuple[str, ...]


def _e(name: str, area: str, line: str, *kinds: str) -> Feature:
    return Feature(name, area, line, "events", kinds)


def _field(name: str, area: str, line: str, event: str, key: str) -> Feature:
    return Feature(name, area, line, "field", (event, key))


def _template_names() -> tuple[str, ...]:
    folder = Path(__file__).parent / "workflow_templates"
    names = []
    for path in sorted(folder.glob("*.json")):
        try:
            names.append(str(json.loads(path.read_text(encoding="utf-8")).get("name") or path.stem))
        except (OSError, ValueError):
            names.append(path.stem)
    return tuple(names)


_CORE: tuple[Feature, ...] = (
    # routing
    Feature("race", "routing", "a start that rarely passes first races the next rung", "moves",
            ("race", "raced_out", "raced_beside")),
    _field("predicted-start", "routing", "a task starts a rung up where its shape failed first tries",
           "planned", "predicted"),
    _field("probation-start", "routing", "a cheaper cell below the ladder tried on trial", "planned", "probation"),
    _e("routed", "routing", "the way a request is read: session, parts or cheaper reading", "routed"),
    _e("split", "routing", "a request that is several tasks cut into pieces", "split_from"),
    _e("dispatch-decision", "routing", "Haiku's call on whether queued work runs beside running work",
       "dispatch_decided"),
    _e("preflight", "routing", "a check that cannot start found before a worker is spent", "preflight"),
    # checks
    _e("independent-check", "checks", "the independent verifier read a change", "verified"),
    _e("verifier-sampling", "checks", "a pass let through unchecked by skip-lot sampling", "verifier_sampled"),
    _e("late-check", "checks", "the verifier reads the kept branch after the ending", "verifier_deferred"),
    _e("verifier-dropped", "checks", "a verifier's verdict dropped because the check failed beside it",
       "verifier_dropped"),
    _e("verifier-wrote", "checks", "what a read-only verifier wrote was put back", "verifier_wrote"),
    _e("check-preexisting", "checks", "a red check compared with the base and found there before",
       "check_preexisting"),
    _e("check-flaky", "checks", "a red check that passed on a rerun", "check_flaky"),
    _e("check-repeated", "checks", "a flake fix whose check ran several times", "check_repeated"),
    _e("check-rewrote", "checks", "files locks' own check wrote, committed and said", "check_rewrote"),
    _e("built-restored", "checks", "a committed build folder put back after the check", "built_restored"),
    _e("review-unread", "checks", "a clean review that read no code, held back once", "review_unread"),
    _e("evidence-owed", "checks", "a pass held back for a neighbour's tool it owed", "evidence_owed"),
    _e("spec-owed", "checks", "a pass held back for the spec a spec-first repository owes", "spec_owed"),
    # trials
    *(Feature(f"trial-{key[:-4]}", "trials", f"briefs drew an arm of the {key[:-4]} trial", "field",
              ("briefed", key)) for key in BRIEFED_ARMS),
    _e("critique", "trials", "a plan read blind before its parts are built", "critiqued"),
    # workflows
    _e("ledger", "workflows", "a review's findings decided row by row", "ledger_decided"),
    _e("integrate", "workflows", "branches of parts joined by an integrate step", "integrated"),
    Feature("parts", "workflows", "a plan fanned out into parts", "option", ("part",)),
    Feature("loop-round", "workflows", "a round of a loop", "option", ("loop",)),
    _e("behind-base", "workflows", "how far a step is behind the branch its run started from", "behind_base"),
    _e("work-carried", "workflows", "failed work finished by the next model in its worktree", "work_carried"),
    _e("chained", "workflows", "a task started on the branch of the work it waited for", "chained"),
    # memory
    _e("notes-filed", "memory", "notes filed from a run or a conversation", "notes_filed"),
    _e("oriented", "memory", "how a passing worker got oriented, kept for the next", "oriented"),
    # habits
    Feature("habit-installed", "habits", "a reviewed habit hook installed", "events", ("habit_installed",)),
    _e("habit-refused", "habits", "a proposed habit refused by locks' own vetting", "habit_refused"),
    Feature("habits", "habits", "habits kept in the store", "table", ("habits", "installed_at", "")),
    # evolution
    Feature("calibration", "evolution", "a standard moved by a calibration", "table", ("calibrations", "at", "repo")),
    Feature("calibration-verdict", "evolution", "a calibration judged against the window before it", "table",
            ("calibration_verdicts", "at", "")),
    Feature("role-revision", "evolution", "a role's lessons proposed, tried or adopted", "table",
            ("role_revisions", "at", "repo")),
    Feature("evolution-snapshot", "evolution", "a day's snapshot of a repository and kind", "table",
            ("evolution", "at", "repo")),
    # session
    _e("compaction", "session", "a compaction advised to the orchestrating session", "compaction_delivered"),
    _e("auto-resume", "session", "a task resumed on its own after a mechanical stop", "auto_resumed"),
    _e("level-changed", "session", "the machine's worker level rose or halved", "level_changed"),
    _e("limit-wait", "session", "the queue held for the account's usage limit", "limit_waiting"),
    _e("copy", "session", "a supervising session heard a copy of work it asked for", "copied"),
    _e("branch-pruned", "session", "a kept branch deleted because nothing was lost", "branch_pruned"),
    _e("branch-set-aside", "session", "a foreign branch of the same name renamed aside", "branch_set_aside"),
    _e("stacks-stopped", "session", "a container stack a worker started stopped at the end", "stacks_stopped"),
    # neighbours
    _e("engram-saved", "neighbours", "what a worker learned saved through engram", "engram_saved"),
)

FEATURES: tuple[Feature, ...] = (
    *_CORE,
    *(Feature(f"kind:{k}", "routing", f"tasks classified as {k}", "kind", (k,)) for k in matrix.KINDS),
    *(Feature(f"workflow:{n}", "workflows", f"runs of the {n} workflow", "workflow", (n,))
      for n in _template_names()),
)


def event_kinds() -> tuple[str, ...]:
    """Every event kind the table names (what the test greps the source for)."""
    out: list[str] = []
    for f in FEATURES:
        if f.how == "events":
            out.extend(f.source)
        elif f.how == "field":
            out.append(f.source[0])
    return tuple(dict.fromkeys(out))


def _cutoff(days: int | None) -> str | None:
    if not days:
        return None
    return (datetime.now(UTC) - timedelta(days=int(days))).isoformat(timespec="seconds")


def _marks(n: int) -> str:
    return ", ".join("?" for _ in range(n))


def _query(f: Feature, since: str | None, repo: str | None) -> tuple[str, list[Any]]:
    """The SQL counting a feature: (uses, tasks it ran on, when last). Every name in it is a
    constant of the table above; every value is a parameter."""
    params: list[Any] = []
    where: list[str] = []
    if f.how in ("events", "field"):
        sel = ("SELECT COUNT(*), COUNT(DISTINCT CASE WHEN e.task_id > 0 THEN e.task_id END), MAX(e.ts) "
               "FROM events e LEFT JOIN tasks t ON t.id = e.task_id")
        if f.how == "events":
            where.append(f"e.kind IN ({_marks(len(f.source))})")
            params += list(f.source)
        else:
            where += ["e.kind = ?", "json_extract(e.data, ?) IS NOT NULL"]
            params += [f.source[0], f"$.{f.source[1]}"]
        if since:
            where.append("e.ts >= ?")
            params.append(since)
        if repo:
            where.append("t.repo = ?")
            params.append(repo)
    elif f.how == "moves":
        sel = ("SELECT COUNT(*), COUNT(DISTINCT a.task_id), MAX(a.created_at) "
               "FROM attempts a JOIN tasks t ON t.id = a.task_id")
        where.append(f"a.move IN ({_marks(len(f.source))})")
        params += list(f.source)
        if since:
            where.append("a.created_at >= ?")
            params.append(since)
        if repo:
            where.append("t.repo = ?")
            params.append(repo)
    elif f.how in ("kind", "option"):
        sel = "SELECT COUNT(*), COUNT(*), MAX(t.created_at) FROM tasks t"
        if f.how == "kind":
            where.append("t.kind = ?")
            params.append(f.source[0])
        else:
            where.append("json_extract(t.options, ?) IS NOT NULL")
            params.append(f"$.{f.source[0]}")
        if since:
            where.append("t.created_at >= ?")
            params.append(since)
        if repo:
            where.append("t.repo = ?")
            params.append(repo)
    elif f.how == "workflow":
        sel = ("SELECT COUNT(DISTINCT w.id), COUNT(t.id), MAX(w.created_at) FROM workflow_runs w "
               "LEFT JOIN tasks t ON t.workflow_run = w.id")
        where.append("w.name = ?")
        params.append(f.source[0])
        if since:
            where.append("w.created_at >= ?")
            params.append(since)
        if repo:
            where.append("w.repo = ?")
            params.append(repo)
    else:  # table
        table, stamp, repo_col = f.source
        sel = f"SELECT COUNT(*), 0, MAX({stamp}) FROM {table}"  # noqa: S608
        if since:
            where.append(f"{stamp} >= ?")
            params.append(since)
        if repo and repo_col:
            where.append(f"{repo_col} = ?")
            params.append(repo)
    return sel + (" WHERE " + " AND ".join(where) if where else ""), params


def report(conn: sqlite3.Connection, days: int | None = None, repo: str | None = None) -> dict[str, Any]:
    """Per feature: how many times it ran, the tasks it ran on, when last (ISO) and its status
    (`used` or `never`), and a summary of features used per area. `repo` is the repository key
    as tasks keep it; the habits table is not kept per repository and ignores it."""
    since = _cutoff(days)
    rows: list[dict[str, Any]] = []
    for f in FEATURES:
        sql, params = _query(f, since, repo)
        try:
            uses, tasks, last = conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:  # a table or column an older copy lacks: it never ran there
            uses, tasks, last = 0, 0, None
        uses = int(uses or 0)
        rows.append({"name": f.name, "area": f.area, "line": f.line, "runs": uses, "tasks": int(tasks or 0),
                     "last": last if uses else None, "status": "used" if uses else "never"})
    areas: dict[str, dict[str, int]] = defaultdict(lambda: {"used": 0, "total": 0})
    for r in rows:
        areas[r["area"]]["total"] += 1
        areas[r["area"]]["used"] += r["status"] == "used"
    ordered = {a: areas[a] for a in AREAS if a in areas}
    return {"days": days, "repo": repo, "features": rows,
            "summary": {"used": sum(r["status"] == "used" for r in rows), "total": len(rows), "areas": ordered}}


def grouped(info: dict[str, Any], *, never_only: bool = False) -> dict[str, list[dict[str, Any]]]:
    """The report's features by area, in the order of AREAS; only the never-used when asked."""
    out: dict[str, list[dict[str, Any]]] = {}
    for a in info["summary"]["areas"]:
        items = [r for r in info["features"] if r["area"] == a and (not never_only or r["status"] == "never")]
        if items:
            out[a] = items
    return out

