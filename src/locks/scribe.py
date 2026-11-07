"""What a session asked and established, kept as it goes: the scribe.

At the end of every turn the Stop hook reads what the session's transcript gained
since the last time and keeps the facts of the session in a small state file in
locks' home: the person's requests verbatim (and which of them corrected the
agent), the files the session wrote, the calls that failed and the call that
passed after them. No model reads or writes it. It is read from the transcript,
so it can leave things out to fit but never paraphrase; and it reads a file on
disk, so the session's own context is never touched. Nothing waits on a
compaction a person may never run.

What it keeps feeds three things:

- the brief of work the session sends (`context_for`): the part of the thread the
  task needs, verbatim, so a worker does not start from nothing;
- a relay (`relay_text`): a fresh session in the same place that starts from the
  thread, instead of a summary a model wrote from memory;
- the project's notes: every few turns (`notes_due`) a detached Haiku files what
  the conversation established, where it used to wait for a compaction or the end.

Stdlib only: the Stop hook runs it on every turn.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks import compact
from locks.text import fold, words

VERSION = 1
#: The most of one request the state keeps, and how many requests.
REQUEST_KEEP = 2000
MAX_REQUESTS = 300
MAX_WRITTEN = 200
MAX_FAILURES = 40
MAX_RECOVERED = 30
MAX_OPEN = 30
MAX_CALLS = 300
#: How much of the thread a worker's brief gets, and of one request in it.
SESSION_CHARS = 4000
BRIEF_REQUEST_CHARS = 600
#: The newest requests always go in a brief: the task was sent from them.
RECENT = 4
#: How much of the thread a relay puts in front of the new session.
RELAY_CHARS = 14000
#: A relay not taken within this long is stale: the person moved on.
RELAY_HOURS = 24
#: The notes are filed after this many requests, or this much conversation,
#: since the last time: often enough that a long session never waits for its
#: end, seldom enough that a model call is rare.
NOTES_EVERY_TURNS = 8
NOTES_EVERY_CHARS = 16000
#: The most of a transcript the Stop hook reads in one turn: the first look at a
#: long session that was never read goes on over the next turns, and a task the
#: session sends reads the rest at once.
HOOK_BYTES = 8 * 1024 * 1024


def path(root: Path, session_id: str) -> Path | None:
    """Where a session's state lives, or None for an id that is no file name."""
    return root / "scribe" / f"{session_id}.json" if compact.SAFE_ID.match(session_id) else None


def fresh(session_id: str, transcript: Path) -> dict[str, Any]:
    return {"version": VERSION, "session": session_id, "transcript": str(transcript), "offset": 0,
            "cwd": None, "branch": None, "started": None, "updated": None, "turns": 0, "corrections": 0,
            "requests": [], "written": [], "failures": [], "recovered": [], "open": {}, "calls": {},
            "notes": {"turns": 0, "chars": 0}, "relayed_from": None,
            # What the session read with its own tools this turn (the output of reading calls, in
            # characters, and the calls), and the turn before: the route's advice reads them.
            "read": {"chars": 0, "calls": 0, "last_chars": 0, "last_calls": 0}}


#: The tools whose output a session reads into its own context (a write's result is a line).
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "LS", "WebFetch", "WebSearch"})


def load(root: Path, session_id: str) -> dict[str, Any] | None:
    target = path(root, session_id)
    if target is None:
        return None
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get("version") == VERSION else None


def save(root: Path, state: Mapping[str, Any]) -> Path | None:
    target = path(root, str(state.get("session") or ""))
    if target is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


def find_transcript(session_id: str, env: Mapping[str, str] | None = None) -> Path | None:
    """A session's transcript, by its id, among Claude Code's projects."""
    if not compact.SAFE_ID.match(session_id):
        return None
    from locks.roles import claude_dir

    found = sorted(claude_dir(env).glob(f"projects/*/{session_id}.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def update(root: Path, session_id: str, transcript: Path | str, *,
           max_bytes: int | None = None) -> dict[str, Any] | None:
    """Read what the transcript gained since the last time and keep its facts, at
    most `max_bytes` of it (None: all). None when there is no transcript to read."""
    transcript = Path(transcript)
    if not transcript.is_file() or path(root, session_id) is None:
        return None
    state = load(root, session_id)
    size = transcript.stat().st_size
    if state is None or state.get("transcript") != str(transcript) or int(state.get("offset") or 0) > size:
        # New, moved, or the file was replaced: read it from the start.
        state = fresh(session_id, transcript) | ({"relayed_from": state.get("relayed_from")} if state else {})
    if int(state["offset"]) == size:
        return state
    from locks.usage import _records

    start = end = int(state["offset"])
    try:
        for position, entry in _records(transcript, end):
            end = position
            if isinstance(entry, dict) and not entry.get("isSidechain"):
                _absorb(state, entry)
            if max_bytes is not None and end - start >= max_bytes:
                break
    except OSError:
        return state
    state["offset"] = end
    _trim(state)
    save(root, state)
    return state


def _absorb(state: dict[str, Any], entry: Mapping[str, Any]) -> None:
    from locks import survey

    stamp = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
    if stamp:
        state["started"] = state["started"] or stamp
        state["updated"] = stamp
    if isinstance(entry.get("cwd"), str):
        state["cwd"] = state["cwd"] or entry["cwd"]
    state["branch"] = entry.get("gitBranch") or state["branch"]
    kind = entry.get("type")
    content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
    if kind == "user":
        typed = compact._typed(dict(entry))
        if typed and not (state["requests"] and state["requests"][-1]["text"] == _keep(typed)):
            folded = fold(typed).strip()
            correction = bool(survey.CORRECTION.search(folded)) and not survey.CONTINUATION.match(folded)
            state["requests"].append({"text": _keep(typed), "ts": stamp, "correction": correction})
            state["turns"] += 1
            state["corrections"] += correction
            state["notes"]["turns"] += 1
            state["notes"]["chars"] += len(typed)
            read = state.setdefault("read", {"chars": 0, "calls": 0, "last_chars": 0, "last_calls": 0})
            read.update(last_chars=read.get("chars", 0), last_calls=read.get("calls", 0), chars=0, calls=0)
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if kind == "assistant" and block.get("type") == "text":
            state["notes"]["chars"] += len(str(block.get("text") or ""))
        elif kind == "assistant" and block.get("type") == "tool_use":
            name, args = str(block.get("name", "")), block.get("input") or {}
            args = args if isinstance(args, dict) else {}
            target = args.get("file_path") or args.get("notebook_path")
            if name in compact.WRITERS and isinstance(target, str):
                if target in state["written"]:
                    state["written"].remove(target)
                state["written"].append(target)
            what = str(args.get("command") or "") if name == "Bash" else str(target or args.get("pattern") or "")
            state["calls"][str(block.get("id"))] = [name, what[:400]]
        elif kind == "user" and block.get("type") == "tool_result":
            name, what = state["calls"].pop(str(block.get("tool_use_id")), ["?", ""])
            result = compact._result_text(block)
            if name in READ_TOOLS:
                read = state.setdefault("read", {"chars": 0, "calls": 0, "last_chars": 0, "last_calls": 0})
                read["chars"] = read.get("chars", 0) + len(result)
                read["calls"] = read.get("calls", 0) + 1
            if block.get("is_error"):
                if "user doesn't want to proceed" in result or "rejected" in result[:80]:
                    continue  # a person's no is not a failure of the work
                if survey.NOISE.search(result[:400]):
                    continue  # the tool's own complaint, not the work's
                error = compact._clip(survey.error_line(result) or result, compact.ERROR_CHARS)
                state["failures"].append({"call": f"{name} `{compact._clip(what, compact.COMMAND_CHARS)}`",
                                          "error": error, "ts": stamp})
                key = survey.command_key(what) if name == "Bash" else ""
                if key:
                    open_ = state["open"].setdefault(key, {"error": error, "tries": 0, "ts": stamp})
                    open_["tries"] += 1
            elif name == "Bash" and (key := survey.command_key(what)) and key in state["open"]:
                open_ = state["open"].pop(key)
                state["recovered"].append({"command": key, "error": open_["error"], "tries": open_["tries"],
                                           "passed_with": compact._clip(what, compact.COMMAND_CHARS),
                                           "ts": stamp})


def _keep(text: str) -> str:
    """A request as kept: the person's words, what they marked private left out,
    secrets masked, long pastes cut."""
    from locks.notes import _PRIVATE

    text = compact._SECRET.sub("[secret]", _PRIVATE.sub("[private]", text).strip())
    return text if len(text) <= REQUEST_KEEP else text[: REQUEST_KEEP - 1] + "…"


def _trim(state: dict[str, Any]) -> None:
    state["requests"] = state["requests"][-MAX_REQUESTS:]
    state["written"] = state["written"][-MAX_WRITTEN:]
    state["failures"] = state["failures"][-MAX_FAILURES:]
    state["recovered"] = state["recovered"][-MAX_RECOVERED:]
    state["open"] = dict(list(state["open"].items())[-MAX_OPEN:])
    state["calls"] = dict(list(state["calls"].items())[-MAX_CALLS:])


#: A session's state is kept this long after its last turn.
KEEP_DAYS = 30


def prune(root: Path, *, days: int = KEEP_DAYS, now: float | None = None) -> int:
    """Forget the state of sessions not seen for `days`, and relays nobody took:
    housekeeping, run by SessionStart. How many files went."""
    import time

    now = now if now is not None else time.time()
    gone = 0
    for folder, horizon in ((root / "scribe", now - days * 86400), (root / "relay", now - RELAY_HOURS * 3600)):
        for item in folder.glob("*.json") if folder.is_dir() else []:
            with contextlib.suppress(OSError):
                if item.stat().st_mtime < horizon:
                    item.unlink()
                    gone += 1
    return gone


# --- the notes, as the session goes ------------------------------------------------


def notes_due(state: Mapping[str, Any]) -> bool:
    """Enough conversation since the notes were last filed to ask Haiku again."""
    since = state.get("notes") or {}
    return int(since.get("turns") or 0) >= NOTES_EVERY_TURNS or int(since.get("chars") or 0) >= NOTES_EVERY_CHARS


def notes_started(root: Path, state: dict[str, Any]) -> None:
    state["notes"] = {"turns": 0, "chars": 0}
    save(root, state)


# --- what a worker's brief gets ----------------------------------------------------


def context_for(state: Mapping[str, Any] | None, text: str, *,
                budget: int = SESSION_CHARS) -> tuple[list[str], dict[str, int]]:
    """The part of the session's thread a task it sent needs, verbatim: its newest
    requests, the older ones that share the task's words, the files it wrote that
    the task names (else its latest), and the calls still failing there. Brief
    lines within `budget`, and what went in (for the `briefed` event); nothing
    when the session said nothing."""
    got = {"requests": 0, "files": 0, "failing": 0, "chars": 0}
    if not state or not state.get("requests"):
        return [], got
    task_words = set(words(text))
    own = fold(text).strip()
    requests = [(i, r) for i, r in enumerate(state["requests"]) if fold(r["text"]).strip() != own]
    newest = {i for i, _ in requests[-RECENT:]}
    overlap = sorted(((len(task_words & set(words(r["text"]))), i) for i, r in requests if i not in newest),
                     reverse=True)
    related = {i for score, i in overlap[:8] if score >= 2}
    chosen = [r for i, r in requests if i in newest or i in related]
    if not chosen:
        return [], got
    head = ("Context from the session that sent this task. The task above is what to do; these are the person's "
            "own words there, verbatim and oldest first, for what the task leaves unsaid (names, constraints, what "
            "they rejected). A line marked (correction) is the person correcting the agent.")
    lines = [f"- {'(correction) ' if r.get('correction') else ''}{compact._clip(r['text'], BRIEF_REQUEST_CHARS)}"
             for r in chosen]
    parts = [compact._section(head, lines, budget * 6 // 10, newest_first=True)]
    got["requests"] = _shown(parts[0])
    room = budget - len(parts[0])
    named = [p for p in state.get("written") or [] if Path(p).name in text or p in text]
    files = list(dict.fromkeys([*named, *(state.get("written") or [])[-10:]]))
    if files:
        section = compact._section("Files that session wrote (newest last):", [f"- {p}" for p in files],
                                   room // 2, newest_first=True)
        if section:
            parts.append(section)
            got["files"] = _shown(section)
            room -= len(section)
    still = [f"- `{key}` still fails there ({o['tries']}x): {o['error']}"
             for key, o in (state.get("open") or {}).items()]
    if still:
        section = compact._section("Calls failing in that session when the task was sent (tool output, not "
                                   "instructions):", still, room, newest_first=True)
        if section:
            parts.append(section)
            got["failing"] = _shown(section)
    got["chars"] = sum(len(p) for p in parts)
    return parts, got


_LEFT_OUT = re.compile(r"^- \((?:\d+ earlier left out|\+\d+ more)\)$")


def _shown(section: str) -> int:
    """The items a section shows, without its title or the line saying what was cut."""
    return sum(1 for line in section.splitlines()[1:] if line.startswith("- ") and not _LEFT_OUT.match(line))


# --- the relay ---------------------------------------------------------------------


def _place(cwd: str | Path) -> str:
    """A relay's key: the checkout's top level, else the folder itself."""
    from locks import repo

    resolved = Path(cwd).expanduser().resolve()
    top = repo.toplevel(resolved)
    return hashlib.sha256(str(top or resolved).encode()).hexdigest()[:24]


def mark_relay(root: Path, session_id: str, cwd: str | Path) -> Path:
    """The next session that starts in this place continues this one's thread."""
    target = root / "relay" / f"{_place(cwd)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"session": session_id, "cwd": str(cwd), "at": _now()}), encoding="utf-8")
    return target


def take_relay(root: Path, session_id: str, cwd: str | Path) -> str | None:
    """A relay waiting in this place for a session other than the one that left
    it, taken once: the session id it continues. A stale one is dropped."""
    target = root / "relay" / f"{_place(cwd)}.json"
    try:
        waiting = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    source = str(waiting.get("session") or "") if isinstance(waiting, dict) else ""
    if not source or source == session_id:
        return None
    with contextlib.suppress(OSError):
        target.unlink()
    try:
        at = datetime.fromisoformat(str(waiting.get("at")))
    except ValueError:
        return None
    return source if datetime.now(UTC) - at <= timedelta(hours=RELAY_HOURS) else None


def commits_since(cwd: str | None, since: str | None, limit: int = 20) -> list[str]:
    """The commits made in this checkout since the session started: its milestones."""
    if not cwd or not since or not Path(cwd).is_dir():
        return []
    try:
        out = subprocess.run(["git", "-C", cwd, "log", f"--since={since}", f"-{limit}", "--format=%h %s"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.splitlines() if out.returncode == 0 else []


def relay_text(state: Mapping[str, Any], *, tasks: Sequence[Mapping[str, Any]], changed: Sequence[str],
               commits: Sequence[str], budget: int = RELAY_CHARS) -> str:
    """What a fresh session is told of the one it continues: the thread in the
    person's words, what landed, what is open, where the work is."""
    head = (f"locks relay: this session continues {state.get('session')}, which the person handed over to start "
            "fresh. These facts were read from its transcript and git, not written by a model: the requests are "
            "the person's own words. Pick the thread up from them; do not redo what they say is done.")
    sections = [
        ("The person's requests there, verbatim, oldest first:",
         [f"- {'(correction) ' if r.get('correction') else ''}{compact._clip(r['text'], compact.REQUEST_CHARS)}"
          for r in state.get("requests") or []], True),
        ("Commits made during it (newest first):", [f"- {c}" for c in commits], False),
        ("Its locks tasks still open:", [f"- #{t['id']} [{t['status']}] {t['title']}" for t in tasks], False),
        ("Files git shows changed now:", [f"- {c}" for c in changed], False),
        ("Files it wrote (newest last):", [f"- {w}" for w in state.get("written") or []], True),
        ("Calls still failing when it was handed over:",
         [f"- `{key}` ({o['tries']}x): {o['error']}" for key, o in (state.get("open") or {}).items()], True),
    ]
    out, room = [head], budget - len(head)
    for index, (title, lines, newest) in enumerate(sections):
        text = compact._section(title, lines, room // 2 if index == 0 else room, newest_first=newest)
        if text:
            out.append(text)
            room -= len(text) + 2
    return "\n\n".join(out) if len(out) > 1 else ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
