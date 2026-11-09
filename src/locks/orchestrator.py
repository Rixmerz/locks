"""Which session orchestrates a project, kept in its `.locks/` so the role is not lost.

The work a session sends reports to that session. When it closes, is cleared
or goes quiet, what it sent kept waiting on a board nobody read, and every stop
that a session could have decided showed as waiting on the person. A project
names one orchestrator in `.locks/orchestrator.json`:

    {"session": "<id>", "cwd": "...", "since": "...", "via": "sent work"}

The first session that sends work becomes it; another one takes it over by
claiming it (`locks orchestrator --claim`), by a relay from it, by a `/clear` in
its place, or by sending work once it has not been seen for `STALE_HOURS`. A
take-over moves the work the old one sent, open or ended unheard, to the new
one (`Store.hand_over`), and so does the orchestrator adopting what a quiet
session left. What a stop needs from the person (`stops.for_person`) still
goes to the person: the orchestrator decides the rest.

Standard library only: the hooks read it on every turn.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FILE = "orchestrator.json"
#: A session not seen this long gives the role up to the next one that sends work,
#: and the work it sent goes to the orchestrator.
STALE_HOURS = 6


def held(folder: Path | None) -> dict[str, Any] | None:
    """The orchestrator a project names, or None."""
    if folder is None:
        return None
    try:
        data = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("session") else None


def is_it(folder: Path | None, session_id: str | None) -> bool:
    found = held(folder)
    return bool(found and session_id and found["session"] == session_id)


def stale(last_seen: str | None, *, now: datetime | None = None) -> bool:
    """A session not seen in `STALE_HOURS` (or never seen) is gone as far as the role goes."""
    if not last_seen:
        return True
    try:
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return True
    return (now or datetime.now(UTC)) - seen > timedelta(hours=STALE_HOURS)


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    with open(folder / f"{FILE}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def claim(folder: Path, session_id: str, cwd: str | None, *, via: str, force: bool = False,
          last_seen: str | None = None, stamp: str | None = None) -> tuple[bool, str | None]:
    """Make `session_id` the orchestrator: when none is named, when it already is,
    when `force` (a person's claim, a relay, a clear in its place), or when the one
    named was not seen in `STALE_HOURS` (`last_seen` is that session's). Returns
    (whether it holds the role now, the session it took it from)."""
    with _locked(folder):
        current = held(folder)
        if current and current["session"] == session_id:
            if current.get("ended"):  # resumed: it is back
                _write(folder, {k: v for k, v in current.items() if k not in ("ended", "end_reason")})
            return True, None
        if current and not force and not current.get("ended") and not stale(last_seen):
            return False, None
        _write(folder, {"session": session_id, "cwd": cwd, "via": via,
                        "since": stamp or datetime.now(UTC).isoformat(timespec="seconds")})
        previous = (current or {}).get("session")
        _traced(session_id, "orchestrator_taken", via=via, cwd=cwd, took_from=previous)
        if previous:
            _traced(previous, "orchestrator_lost", to=session_id, via=via, cwd=cwd)
        return True, previous


def _traced(session_id: str, kind: str, **data: Any) -> None:
    """The role's move in the person's trace of each session it touched (`trace.action`): a session
    that took it, or lost it, says so where an audit of that session reads. Never raises."""
    from locks import trace

    trace.action(os.environ, session_id, kind, **data)


def _write(folder: Path, entry: dict[str, Any]) -> None:
    tmp = folder / f"{FILE}.tmp"
    tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(folder / FILE)


def ended(folder: Path | None, session_id: str | None, reason: str, *, stamp: str | None = None) -> bool:
    """The orchestrator's session ended (closed, cleared): the role stays named, marked
    ended, for the next session in its place to take with its work. True if marked."""
    if folder is None or not session_id:
        return False
    with _locked(folder):
        current = held(folder)
        if not current or current["session"] != session_id:
            return False
        _write(folder, {**current, "ended": stamp or datetime.now(UTC).isoformat(timespec="seconds"),
                        "end_reason": reason or "other"})
        _traced(session_id, "orchestrator_ended", reason=reason or "other", cwd=current.get("cwd"))
        return True


def started(store: Any, folder: Path | None, session_id: str | None, cwd: str | None) -> tuple[bool, int]:
    """A session started: it takes the role when the orchestrator's session ended in
    this same place (a `/clear`, a closed terminal opened again), or it is that
    session resumed. (whether it holds the role, how many tasks moved)."""
    current = held(folder)
    if folder is None or not session_id or not current:
        return False, 0
    if current["session"] == session_id:
        return take(store, folder, session_id, cwd, via="resumed")
    if current.get("ended") and cwd and _same(current.get("cwd"), cwd):
        return take(store, folder, session_id, cwd, via=f"after {current.get('end_reason') or 'its end'}",
                    force=True)
    return False, 0


def _same(a: str | None, b: str) -> bool:
    try:
        return bool(a) and Path(str(a)).resolve() == Path(b).resolve()
    except OSError:
        return False


def release(folder: Path, session_id: str | None = None) -> bool:
    """Drop the role: only the holder's own release, or any with no session named."""
    with _locked(folder):
        current = held(folder)
        if not current or (session_id and current["session"] != session_id):
            return False
        with contextlib.suppress(OSError):
            (folder / FILE).unlink()
        return True


def take(store: Any, folder: Path | None, session_id: str | None, cwd: str | None, *, via: str,
         force: bool = False) -> tuple[bool, int]:
    """Claim the role (as `claim`) and bring the work the session it was taken from
    sent: (whether this session holds it, how many tasks moved). Never fails the
    caller: a project with no `.locks/` has no orchestrator to name."""
    if folder is None or not session_id:
        return False, 0
    current = held(folder)
    seen = None
    if current and current["session"] != session_id:
        seen = (store.get_session(current["session"]) or {}).get("last_seen_at")
    try:
        holds, previous = claim(folder, session_id, cwd, via=via, force=force, last_seen=seen)
    except OSError:
        return False, 0
    moved = store.hand_over(previous, session_id) if holds and previous else 0
    return holds, moved


def adopt(store: Any, repos: list[str], session_id: str, *, now: datetime | None = None) -> int:
    """The orchestrator takes the work that sessions gone quiet (`STALE_HOURS`) sent
    in its project: open, waiting, or ended unheard. How many tasks moved."""
    before = ((now or datetime.now(UTC)) - timedelta(hours=STALE_HOURS)).isoformat(timespec="seconds")
    moved = 0
    for quiet in store.quiet_senders(repos, before, but=session_id):
        moved += store.hand_over(quiet, session_id, repos=repos)
    return moved


def sent(store: Any, where: str | Path, env: dict[str, str]) -> tuple[bool, int]:
    """A session sent work here: it becomes the project's orchestrator unless a live
    one holds the role. A worker never does (it starts no work), nor a terminal."""
    session = env.get("LOCKS_SESSION_ID")
    if not session or env.get("LOCKS_WORKER_TASK"):
        return False, 0
    from locks import project

    return take(store, project.find(where), session, str(where), via="sent work")
