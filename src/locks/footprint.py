"""The fixed weight of a worker's brief, per kind and release (`locks footprint`).

Every `briefed` event records the characters of the context locks put in front of the
task and of the role's system prompt, with the release that wrote it. This reads them back as the
median per kind and release, and says where a release made a kind's brief `GROWTH` times heavier
than the one before it: said, never acted on. Read only, no model.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from itertools import pairwise
from statistics import median
from typing import Any

#: A release whose median brief is this many times the one before it is named.
GROWTH = 1.2


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


def report(conn: sqlite3.Connection, *, days: int | None = None) -> dict[str, Any]:
    """Per kind: each release's median context and system characters, the tasks counted, and
    the releases that grew it past `GROWTH`."""
    query = "SELECT data FROM events WHERE kind = 'briefed'"
    args: tuple = ()
    if days:
        query += " AND ts >= datetime('now', ?)"
        args = (f"-{int(days)} days",)
    rows: dict[str, dict[str, list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    for (raw,) in conn.execute(query, args):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        kind, version = str(data.get("kind") or "?"), str(data.get("version") or "?")
        rows[kind][version].append((int(data.get("context_chars") or 0), int(data.get("system_chars") or 0)))
    out: dict[str, Any] = {}
    for kind, by_version in sorted(rows.items()):
        releases = []
        for version in sorted(by_version, key=_version_key):
            got = by_version[version]
            releases.append({"version": version, "tasks": len(got),
                             "context_chars": int(median(c for c, _ in got)),
                             "system_chars": int(median(s for _, s in got))})
        grew = []
        for before, after in pairwise(releases):
            was = before["context_chars"] + before["system_chars"]
            now = after["context_chars"] + after["system_chars"]
            if was and now >= was * GROWTH:
                grew.append({"from": before["version"], "to": after["version"], "ratio": round(now / was, 2)})
        out[kind] = {"releases": releases, "grew": grew}
    return out
