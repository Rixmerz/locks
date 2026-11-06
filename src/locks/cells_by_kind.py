"""Which model and effort fit each kind of work, as locks starts it and as it measured it here
(`locks cells`): for the session that delegates, so it knows where work lands without guessing.

Per kind: the ladder (`matrix.LADDERS`), the cell locks starts it on in this repository (a calibrated
standard at any size, else the ladder's first rung), and per cell the first tries it passed here
(`standards.rates`), with what a passed point cost and took. Read only, no model: a table of what
happened, never a rule. locks still chooses the cell of every task it runs; the table is for the
session's own subagents and for knowing what a kind costs before sending it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from locks import matrix, standards
from locks.store import Store

#: First tries a cell needs before its rate is shown as a rate rather than a count.
SHOWN_FROM = 3


def report(store: Store, repo: str | None) -> list[dict[str, Any]]:
    """One row per kind: its ladder, where it starts here and why, and each cell's record."""
    ended = standards.ended(store, repo=repo)
    rows = []
    for kind in matrix.KINDS:
        tasks = [t for t in ended if t.kind == kind]
        ladder = [c.label for c in matrix.ladder_for(kind)]
        start, why = ladder[0], "the ladder's first rung"
        if repo:
            row = store.standard(repo, kind, standards.ANY_SIZE) or {}
            if row.get("start_cell") and row["start_cell"] in ladder:
                start, why = row["start_cell"], "calibrated here from first tries"
        measured = standards.rates(tasks)
        cells = []
        for label in dict.fromkeys([*ladder, *measured]):
            passed, tried = measured.get(label, [0, 0])
            cells.append({"cell": label, "first_tries": tried, "passed": passed,
                          "rate": round(passed / tried, 2) if tried else None, "on_ladder": label in ladder})
        m = standards.metrics(tasks) if tasks else {}
        rows.append({"kind": kind, "read_only": kind in matrix.READ_ONLY_KINDS, "ladder": ladder,
                     "start": start, "why": why, "tasks": len(tasks), "pass_first": m.get("pass_first"),
                     "cost_per_point": m.get("cost_per_point"), "time_per_point_s": m.get("time_per_point_s"),
                     "cells": cells})
    return rows


def best(row: dict[str, Any]) -> str | None:
    """The cheapest cell of the ladder whose first tries here passed at least `standards.PASS_FIRST`
    over `standards.MIN_SAMPLES`; None when none has the evidence."""
    for c in row["cells"]:
        if c["on_ladder"] and c["first_tries"] >= standards.MIN_SAMPLES and (c["rate"] or 0) >= standards.PASS_FIRST:
            return str(c["cell"])
    return None


def _cell_text(c: dict[str, Any]) -> str:
    if not c["first_tries"]:
        return f"{c['cell']} (no tries)"
    if c["first_tries"] < SHOWN_FROM:
        return f"{c['cell']} {c['passed']}/{c['first_tries']}"
    return f"{c['cell']} {round(100 * c['rate'])}% of {c['first_tries']}"


def text(rows: Sequence[dict[str, Any]], where: str | None) -> list[str]:
    lines = [f"Where each kind starts{' in ' + where if where else ''}, and its first tries per cell "
             "(locks picks the cell of every task it runs; this is for your own subagents and for knowing "
             "what a kind costs):"]
    for row in rows:
        head = f"  {row['kind']:<16} {'read ' if row['read_only'] else 'write'}  starts {row['start']:<14}"
        found = best(row)
        tail = f" proven here: {found}" if found else ""
        lines.append(head + f" ({row['why']}){tail}")
        if row["tasks"]:
            cost = f", ${row['cost_per_point']:.3f}/point" if row["cost_per_point"] is not None else ""
            first = f", first try {round(100 * row['pass_first'])}%" if row["pass_first"] is not None else ""
            lines.append(f"  {'':<16}        {row['tasks']} task(s){first}{cost}: "
                         + "; ".join(_cell_text(c) for c in row["cells"] if c["first_tries"]))
    lines.append("For a subagent of your own (the Agent tool's model): reading kinds start on the ladder's "
                 "model shown; a kind with nothing proven here goes by its ladder, never cheaper.")
    return lines
