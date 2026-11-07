"""Forensics of a store (`locks forensics`): where the money, the cache and the friction went.

Read from a copy of the database, never the live file: `snapshot` takes one with SQLite's own
backup into a temporary file, or a person passes a copy with `--db`. Every figure is
**observed** (counted from rows: attempts, usage, tool events, the trace's records), and the one
derived figure, the cost of attempts that did not pass, is named as such; nothing is modeled or
replayed. No model, no network.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from locks import footprint, notes_fork, trace


def snapshot(live: Path) -> Path:
    """A consistent copy of the live store, through SQLite's backup, in a temporary file."""
    target = Path(tempfile.mkdtemp(prefix="locks-forensics-")) / "copy.db"
    source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target


def open_copy(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _since(days: int) -> str:
    return f"-{int(days)} days"


def spend(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    """Per kind and cell: attempts, passed, dollars, and the dollars of attempts that did not pass."""
    rows = conn.execute(
        "SELECT COALESCE(t.kind, '?'), a.cell, COUNT(*), SUM(a.passed), SUM(a.cost_usd), "
        "SUM(CASE WHEN a.passed = 0 THEN a.cost_usd ELSE 0 END), SUM(a.turns) "
        "FROM attempts a JOIN tasks t ON t.id = a.task_id WHERE a.created_at >= datetime('now', ?) "
        "GROUP BY 1, 2 ORDER BY 5 DESC", (_since(days),)).fetchall()
    cells = [{"kind": k, "cell": c, "attempts": n, "passed": int(p or 0), "usd": round(u or 0, 4),
              "unpassed_usd": round(w or 0, 4), "turns": int(t or 0)} for k, c, n, p, u, w, t in rows]
    total = sum(c["usd"] for c in cells)
    unpassed = sum(c["unpassed_usd"] for c in cells)
    return {"cells": cells, "usd": round(total, 4), "unpassed_usd": round(unpassed, 4),
            "unpassed_share": round(unpassed / total, 3) if total else None}


def cache(conn: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    """Per model: input served from the cache against written to it and paid in full. From the
    sessions' usage (read from their transcripts); without any, from the workers' attempts, which
    keep the same counts on `attempt_finished` (`source: attempts`, one row per attempt)."""
    out = []
    for model, inp, read, write, n in conn.execute(
            "SELECT model, SUM(input_tokens), SUM(cache_read), SUM(cache_write), COUNT(*) FROM usage "
            "WHERE ts >= datetime('now', ?) GROUP BY model ORDER BY 3 DESC", (_since(days),)):
        whole = (inp or 0) + (read or 0) + (write or 0)
        out.append({"model": model or "?", "messages": n, "input": inp or 0, "cache_read": read or 0,
                    "cache_write": write or 0, "read_share": round((read or 0) / whole, 3) if whole else None})
    if out:
        return out
    for model, inp, read, write, n in conn.execute(
            "SELECT json_extract(data, '$.served_model'), SUM(json_extract(data, '$.input_tokens')), "
            "SUM(json_extract(data, '$.cache_read_tokens')), SUM(json_extract(data, '$.cache_write_tokens')), "
            "COUNT(*) FROM events WHERE kind = 'attempt_finished' AND ts >= datetime('now', ?) "
            "GROUP BY 1 ORDER BY 3 DESC", (_since(days),)):
        # An attempt's input counts every token; its cache reads and writes are the part served or stored.
        paid = max(0, (inp or 0) - (read or 0) - (write or 0))
        whole = inp or 0
        out.append({"model": model or "?", "messages": n, "input": paid, "cache_read": read or 0,
                    "cache_write": write or 0, "read_share": round((read or 0) / whole, 3) if whole else None,
                    "source": "attempts"})
    return out


def friction(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    """Tool calls that failed, by tool, and calls repeated back to back with the same arguments
    (the same signature and argument hash in one session and attempt)."""
    failed: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    repeats: Counter[str] = Counter()
    last: dict[tuple, tuple] = {}
    for sid, task, attempt, tool, sig, arg, ok in conn.execute(
            "SELECT session_id, task_id, attempt, tool, sig, arg_hash, ok FROM tool_events "
            "WHERE ts >= datetime('now', ?) ORDER BY id", (_since(days),)):
        name = tool or "?"
        calls[name] += 1
        if not ok:
            failed[name] += 1
        where = (sid, task, attempt)
        if last.get(where) == (sig, arg):
            repeats[name] += 1
        last[where] = (sig, arg)
    tools = [{"tool": t, "calls": n, "failed": failed[t], "repeated": repeats[t],
              "failed_share": round(failed[t] / n, 3)} for t, n in calls.most_common()]
    return {"calls": sum(calls.values()), "failed": sum(failed.values()), "repeated": sum(repeats.values()),
            "tools": tools}


def traced(env: Mapping[str, str], limit: int = 50) -> dict[str, Any]:
    """What the opt-in trace shows across its newest sessions: livespec tools called, context injected."""
    sessions = trace.sessions(env)[:limit]
    livespec: Counter[str] = Counter()
    injected = 0
    for row in sessions:
        info = trace.summary(env, row["session_id"])
        livespec.update(info["livespec_calls"])
        injected += info["injected"]
    return {"sessions": len(sessions), "livespec_calls": dict(livespec.most_common()), "injected": injected}


def notes_trial(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = defaultdict(dict)
    for key, value in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'notes_fork:%'"):
        try:
            out[key.split(":", 1)[1]] = json.loads(value)
        except ValueError:
            continue
    return {"arms": notes_fork.ARMS, "projects": dict(out)}


def report(conn: sqlite3.Connection, env: Mapping[str, str], *, days: int = 30) -> dict[str, Any]:
    return {"label": "observed", "days": days, "spend": spend(conn, days), "cache": cache(conn, days),
            "friction": friction(conn, days), "footprint": footprint.report(conn, days=days),
            "notes_fork": notes_trial(conn), "trace": traced(env)}
