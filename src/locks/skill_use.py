"""How the skills locks' roles carry are used (`locks skills`), and their lifecycle.

Each brief keeps the skills its role carried (`briefed.skill_sources`). This counts, per skill,
the briefs that carried it, the first tries of those tasks that passed, and when it was last
carried, and gives it a state: `active` while it was carried in the last `STALE_DAYS`, `stale`
until `ARCHIVE_DAYS`, then `dormant`. A dormant skill is said, never moved or deleted: a role's
files are the person's and locks', and only a person changes them. A skill the person pinned
(`locks skills --pin NAME`, kept in their locks home) is always `pinned`. Read only, no model.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STALE_DAYS = 14
ARCHIVE_DAYS = 30
PINS = "skill-pins.json"


def pins(root: Path) -> set[str]:
    try:
        got = json.loads((root / PINS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(n) for n in got} if isinstance(got, list) else set()


def pin(root: Path, name: str, *, on: bool = True) -> set[str]:
    held = pins(root)
    (held.add if on else held.discard)(name)
    root.mkdir(parents=True, exist_ok=True)
    (root / PINS).write_text(json.dumps(sorted(held), indent=2), encoding="utf-8")
    return held


def _when(raw: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def report(conn: sqlite3.Connection, root: Path, *, now: datetime | None = None,
           known: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """One row per skill: briefs, first tries passed of them, last carried, state. `known` adds
    the skills roles carry that no brief has yet (name -> source)."""
    now = now or datetime.now(UTC)
    first: dict[int, int] = {int(t): int(p) for t, p in conn.execute(
        "SELECT task_id, passed FROM attempts a WHERE seq = (SELECT MIN(seq) FROM attempts b "
        "WHERE b.task_id = a.task_id)")}
    rows: dict[str, dict[str, Any]] = {}
    for task_id, ts, raw in conn.execute("SELECT task_id, ts, data FROM events WHERE kind = 'briefed'"):
        try:
            sources = json.loads(raw).get("skill_sources") or {}
        except (ValueError, AttributeError):
            continue
        if not isinstance(sources, dict):
            continue
        for name, source in sources.items():
            row = rows.setdefault(name, {"skill": name, "source": source, "briefs": 0, "tries": 0, "passed": 0,
                                         "last": None})
            row["briefs"] += 1
            if int(task_id) in first:
                row["tries"] += 1
                row["passed"] += first[int(task_id)]
            if row["last"] is None or ts > row["last"]:
                row["last"], row["source"] = ts, source
    for name, source in (known or {}).items():
        rows.setdefault(name, {"skill": name, "source": source, "briefs": 0, "tries": 0, "passed": 0, "last": None})
    held = pins(root)
    for row in rows.values():
        last = _when(row["last"]) if row["last"] else None
        age = (now - last) if last else None
        row["pass_first"] = round(row["passed"] / row["tries"], 3) if row["tries"] else None
        if row["skill"] in held:
            row["state"] = "pinned"
        elif age is not None and age <= timedelta(days=STALE_DAYS):
            row["state"] = "active"
        elif age is not None and age <= timedelta(days=ARCHIVE_DAYS):
            row["state"] = "stale"
        else:
            row["state"] = "dormant"
    return sorted(rows.values(), key=lambda r: (-r["briefs"], r["skill"]))
