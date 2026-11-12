"""What work really cost: tokens per model, read from the session's own log.

Workers report their cost in the JSON envelope `claude -p` prints, and every
attempt already stores it. A session's own turns are the other half: Claude
Code writes each API response's usage into the session transcript, under the
model that actually served it. This module reads that file incrementally at
`Stop` — from the offset it reached last time, complete lines only, one record
per message id (a streamed message repeats its usage on several lines).

A subagent writes its own transcript beside the session's, under
`<session>/subagents/agent-<id>.jsonl`, with a `.meta.json` naming the tool
call that started it. Its turns are paid like the session's own, so they are
read at the same `Stop` and counted to the delegation that call opened.

The numbers have two uses: the spend screen, and the router. `expected_cost`
is the average a finished task of a kind cost in this repository, so `locks
route` can say what a choice is likely to cost before it is paid.

Dollar figures are first-party API rates applied to the token counts: on a
subscription they measure how much of the plan a piece of work used.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from locks.matrix import MODELS
from locks.store import Store

#: Cache reads bill at a tenth of the input rate; cache writes at 1.25 times.
CACHE_READ, CACHE_WRITE = 0.1, 1.25
#: A line longer than this is skipped, not read: one runaway line must not stop
#: every later line from being counted.
MAX_LINE = 32 * 1024 * 1024


def family(model: str | None) -> str:
    name = (model or "").lower()
    for alias in MODELS:
        if alias in name:
            return alias
    return "other"


def dollars(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    rates = MODELS.get(family(model))
    if rates is None:
        return 0.0
    paid_input = input_tokens + cache_read * CACHE_READ + cache_write * CACHE_WRITE
    return (paid_input * rates.input_usd_per_mtok + output_tokens * rates.output_usd_per_mtok) / 1_000_000


def _records(path: Path, offset: int) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """(offset after the line, parsed entry) for every complete line past `offset`."""
    with path.open("rb") as fh:
        fh.seek(offset)
        position = offset
        while True:
            line = fh.readline(MAX_LINE + 1)
            if not line:
                return
            if not line.endswith(b"\n"):
                if len(line) > MAX_LINE:
                    # Too long to read whole: skip to the end of it.
                    rest = fh.readline()
                    while rest and not rest.endswith(b"\n"):
                        rest = fh.readline()
                    position = fh.tell()
                    yield position, None
                    continue
                return  # a line still being written; read it next time
            position += len(line)
            try:
                yield position, json.loads(line)
            except ValueError:
                yield position, None


#: Where a session's last count is kept (in the store's meta): when, by which release, and the transcript.
COUNTED_KEY = "usage-counted:"
#: A session cost figure counted longer ago than this is said with its age, never presented as current.
STALE_MINUTES = 10
#: A transcript this many bytes past what was ingested (and idle for `STALE_MINUTES`) is a Stop that did
#: not do its work (`locks doctor`).
BEHIND_BYTES = 1024 * 1024


def mark_counted(store: Store, session_id: str, transcript: str | Path, *, now: datetime | None = None) -> None:
    """Record that this session's usage was counted now, for the cost line and `locks doctor`."""
    from locks import __version__

    store.set_meta(COUNTED_KEY + session_id, json.dumps(
        {"at": (now or datetime.now(UTC)).isoformat(), "version": __version__, "transcript": str(transcript)}))


def counted(store: Store, session_id: str) -> dict[str, Any] | None:
    """The record `mark_counted` kept, or None when the session has none."""
    try:
        found = json.loads(store.meta(COUNTED_KEY + session_id) or "")
    except ValueError:
        return None
    return found if isinstance(found, dict) and found.get("at") else None


def ingest(store: Store, transcript: str | Path, *, session_id: str, task_id: int | None, repo: str | None) -> int:
    """Count the new assistant messages of one transcript. Returns how many."""
    path = Path(transcript)
    if not path.is_file():
        return 0
    offset = store.transcript_offset(str(path))
    if offset > path.stat().st_size:
        offset = 0  # the file was replaced
    seen: dict[str, dict[str, Any]] = {}
    end = offset
    for position, entry in _records(path, offset):
        end = position
        if not entry or entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        if not message.get("id") or not usage:
            continue
        seen[message["id"]] = {
            "message_id": message["id"],
            "session_id": entry.get("sessionId") or session_id,
            "task_id": task_id,
            "repo": repo,
            "model": message.get("model"),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
            "sidechain": int(bool(entry.get("isSidechain"))),
            "ts": entry.get("timestamp"),
        }
    for record in seen.values():
        store.add_usage(**record)
    store.set_transcript_offset(str(path), end)
    return len(seen)


def subagents_folder(transcript: str | Path) -> Path:
    """Where a session's subagents write their transcripts."""
    return Path(transcript).with_suffix("") / "subagents"


def subagent_tool_use(transcript: str | Path, agent_id: str) -> str | None:
    """The tool call that started a subagent, from the meta file beside its transcript."""
    meta = subagents_folder(transcript) / f"agent-{agent_id}.meta.json"
    try:
        tool_use = json.loads(meta.read_text(encoding="utf-8")).get("toolUseId")
    except (OSError, ValueError, AttributeError):
        return None
    return tool_use if isinstance(tool_use, str) else None


def ingest_subagents(store: Store, transcript: str | Path, *, session_id: str, task_id: int | None,
                     repo: str | None) -> int:
    """Count the new messages of every subagent of a session; each goes to the delegation
    its tool call opened, else to `task_id`. Returns how many."""
    folder = subagents_folder(transcript)
    if not folder.is_dir():
        return 0
    count = 0
    for path in sorted(folder.glob("agent-*.jsonl")):
        tool_use = subagent_tool_use(transcript, path.stem.removeprefix("agent-"))
        child = store.delegation(session_id, tool_use) if tool_use else None
        count += ingest(store, path, session_id=session_id, task_id=child["id"] if child else task_id,
                        repo=(child or {}).get("repo") or repo)
    return count


def transcript_of(session_id: str, env: Mapping[str, str] | None = None) -> Path | None:
    """Where Claude Code keeps a session's transcript (`projects/<folder>/<id>.jsonl` in its
    config folder), or None when it is not there."""
    env = dict(os.environ) if env is None else env
    if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
        return None
    root = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else Path(
        env.get("HOME") or Path.home()).expanduser() / ".claude"
    found = sorted(root.glob(f"projects/*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def transcript_cost(path: Path, *, since: str | None = None) -> float:
    """What the assistant messages of one transcript cost so far, each message once; with
    `since` (an ISO time), only the ones written from then on (a resumed session's file
    holds the attempts before it too)."""
    seen: dict[str, float] = {}
    try:
        for _, entry in _records(path, 0):
            if not entry or entry.get("type") != "assistant":
                continue
            # Both are UTC ISO times; to the second, whatever their zone suffix says.
            if since and str(entry.get("timestamp") or "")[:19] < since[:19]:
                continue
            message = entry.get("message") or {}
            usage = message.get("usage") or {}
            if not message.get("id") or not usage:
                continue
            seen[message["id"]] = dollars(
                str(message.get("model") or ""), int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0),
                int(usage.get("cache_creation_input_tokens") or 0))
    except OSError:
        return 0.0
    return round(sum(seen.values()), 4)


def transcript_turns(path: Path, *, since: str | None = None) -> int:
    """How many model calls one transcript holds (each assistant message once, by its id); with
    `since`, only the ones written from then on. 0 when it cannot be read."""
    seen: set[str] = set()
    try:
        for _, entry in _records(path, 0):
            if not entry or entry.get("type") != "assistant":
                continue
            if since and str(entry.get("timestamp") or "")[:19] < since[:19]:
                continue
            if mid := (entry.get("message") or {}).get("id"):
                seen.add(str(mid))
    except OSError:
        return 0
    return len(seen)


def running_cost(task_id: int, attempt: int | None, *, since: str | None = None,
                 env: Mapping[str, str] | None = None) -> float | None:
    """What a running attempt cost so far, read from its worker's live transcript: its session
    is the one its calls name (`phase.session_of`). None when it cannot be found: a cost
    nobody read is never shown as $0.00. Read only."""
    from locks import phase

    session = phase.session_of(task_id, attempt, env)
    path = transcript_of(session, env) if session else None
    return transcript_cost(path, since=since) if path is not None else None


def so_far(store: Store, task: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A task's workers' cost as a view shows it: what its finished attempts cost, plus, while
    one runs, what its live transcript says so far (`cost_live`); `cost_running` when the
    running attempt's cost cannot be read, so a view says "running", never $0.00."""
    spent = float(task.get("cost_usd") or 0)
    if task.get("status") != "running":
        return {"cost_usd": round(spent, 2)}
    started = store.last_event(task["id"], "attempt_started")
    seq = (started or {}).get("data", {}).get("seq") if started else None
    ended = store.last_event(task["id"], "attempt_finished")
    if started is None or (ended is not None and ended["id"] > started["id"]):
        return {"cost_usd": round(spent, 2)}  # between attempts: every one so far is counted
    live = running_cost(task["id"], seq, since=started["ts"], env=env)
    if live is None:
        return {"cost_usd": round(spent, 2), "cost_running": True}
    return {"cost_usd": round(spent + live, 2), "cost_live": True}


def cost_by(store: Store, column: str, values: list[Any]) -> dict[Any, float]:
    """What sessions' own turns cost, subagents included, per task id or per session id."""
    totals: dict[Any, float] = {}
    for row in store.usage_by(column, values):
        totals[row["key"]] = totals.get(row["key"], 0.0) + dollars(
            row["model"], row["input_tokens"], row["output_tokens"], row["cache_read"], row["cache_write"])
    return {k: round(v, 4) for k, v in totals.items()}


def spend(store: Store, *, days: int = 7, repo: str | None = None) -> dict[str, Any]:
    """Tokens and dollars by model, for sessions and for workers, over `days`."""
    sessions = store.usage_by_model(days=days, repo=repo)
    for row in sessions:
        row["usd"] = round(dollars(row["model"], row["input_tokens"], row["output_tokens"],
                                   row["cache_read"], row["cache_write"]), 4)
        row["family"] = family(row["model"])
    workers = store.worker_spend(days=days, repo=repo)
    return {
        "days": days,
        "sessions": sessions,
        "workers": workers,
        "total_usd": round(sum(r["usd"] for r in sessions) + sum(w["usd"] for w in workers), 4),
    }
