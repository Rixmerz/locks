"""The observability trace (`trace` setting, off by default): every hook event, tool call and
locks event of a session, tagged, in `$LOCKS_HOME/traces/<session>.jsonl`, and a copy of the
session's transcript beside it when it stops (`<session>.transcript.jsonl`, where its tokens are).

It answers whether what locks claims happens really happens: was livespec indexed, which livespec
tools a model called, which notes a brief recalled, what context a hook injected, what each turn
cost. Unlike the tool-call log (`locks.signature`), it keeps arguments and outputs, so it is the
person's opt-in and lives in their locks home only; secrets are masked. `locks trace` reads it.

Every record has a `tag`: `hook:<Event>` for a hook Claude Code ran (its tool's name as `tool`),
`tool:<name>` for a call's input and output, `inject:<Event>` for the context a hook gave back,
`locks:<event>` for an event locks recorded on a task (`briefed`, `planned`, `attempt_finished`…).
This module runs on every tool call while on: standard library only, nothing of locks' store.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The folder in the locks home that holds the traces.
DIR = "traces"
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|"
                     r"xox[abposr]-[\w-]{8,}|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{30,}|"
                     r"-----BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+|"
                     r"bearer\s+[A-Za-z0-9._~+/-]{20,})", re.IGNORECASE)
_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}
#: A livespec tool as Claude Code names it: `mcp__<server with livespec>__<tool>`.
_LIVESPEC = re.compile(r"^mcp__(?:[^_]|_(?!_))*livespec(?:[^_]|_(?!_))*__(.+)$", re.IGNORECASE)


def home(env: Mapping[str, str]) -> Path:
    from locks import legacy

    if given := legacy.getenv(env, "LOCKS_HOME"):
        return Path(given).expanduser()
    xdg = env.get("XDG_DATA_HOME", "")
    return legacy.home(Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "share")


def enabled(env: Mapping[str, str]) -> bool:
    """`LOCKS_TRACE`, else `config.json`'s `trace`, else off. Read without `locks.config`,
    which imports the store: this runs on every tool call."""
    raw = (env.get("LOCKS_TRACE") or "").strip().lower()
    if raw in _TRUE | _FALSE:
        return raw in _TRUE
    try:
        stored = json.loads((home(env) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(stored, dict) and stored.get("trace") is True


def mask(value: Any) -> Any:
    """`value` with every string's secrets masked, its shape kept."""
    if isinstance(value, str):
        return _SECRET.sub("[secret]", value)
    if isinstance(value, Mapping):
        return {str(k): mask(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask(v) for v in value]
    return value


def path(env: Mapping[str, str], session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "") or "no-session"
    return home(env) / DIR / f"{safe}.jsonl"


def write(env: Mapping[str, str], session_id: str | None, tag: str, **data: Any) -> None:
    """Append one tagged record to the session's trace, in one write."""
    task, attempt = env.get("LOCKS_WORKER_TASK"), env.get("LOCKS_WORKER_ATTEMPT")
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "tag": tag,
              "session_id": session_id,
              "worker_task": int(task) if task and task.isdigit() else None,
              "worker_attempt": int(attempt) if attempt and attempt.isdigit() else None,
              **mask(data)}
    target = path(env, session_id or "")
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


#: Hook events traced by their own entry (`locks hook-trace`) or the PostToolUse fast path, never
#: again by the full hook.
OWN_PATH = frozenset({"PreToolUse", "PostToolUse"})


def hook(event_name: str, event: Mapping[str, Any], env: Mapping[str, str]) -> None:
    """A hook Claude Code ran, whole: its tool's call (input, and output after it) under `tool:`."""
    sid = str(event.get("session_id") or "")
    tool = event.get("tool_name")
    rest = {k: v for k, v in event.items() if k not in {"session_id", "tool_input", "tool_response"}}
    write(env, sid, f"hook:{event_name}", **rest)
    if tool:
        livespec = _LIVESPEC.match(str(tool))
        write(env, sid, f"tool:{tool}", phase=event_name, tool_use_id=event.get("tool_use_id"),
              livespec_tool=livespec.group(1) if livespec else None,
              input=event.get("tool_input"),
              **({"response": event.get("tool_response")} if "tool_response" in event else {}))


def injected(event_name: str, event: Mapping[str, Any], answer: Any, env: Mapping[str, str]) -> None:
    """What a hook gave back to the session (context, a block, a decision)."""
    if answer:
        write(env, str(event.get("session_id") or ""), f"inject:{event_name}", answer=answer)


def locks_event(env: Mapping[str, str], session_id: str | None, task_id: int, kind: str,
                data: Mapping[str, Any]) -> None:
    """An event locks recorded on a task, under the session that sent it."""
    write(env, session_id, f"locks:{kind}", task_id=task_id, data=dict(data))


def keep_transcript(event: Mapping[str, Any], env: Mapping[str, str]) -> Path | None:
    """A copy of the session's transcript beside its trace (tokens per message are there)."""
    source = event.get("transcript_path")
    sid = str(event.get("session_id") or "")
    if not source or not sid or not Path(str(source)).is_file():
        return None
    target = path(env, sid).with_suffix(".transcript.jsonl")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source), target)
    target.chmod(0o600)
    return target


# --- reading ---------------------------------------------------------------


def sessions(env: Mapping[str, str]) -> list[dict[str, Any]]:
    """The traced sessions, newest first."""
    folder = home(env) / DIR
    if not folder.is_dir():
        return []
    out = []
    for p in folder.glob("*.jsonl"):
        if p.name.endswith(".transcript.jsonl"):
            continue
        stat = p.stat()
        out.append({"session_id": p.stem, "path": str(p), "bytes": stat.st_size,
                    "updated": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
                    "transcript": p.with_suffix(".transcript.jsonl").is_file()})
    return sorted(out, key=lambda r: r["updated"], reverse=True)


def records(env: Mapping[str, str], session_id: str) -> list[dict[str, Any]]:
    out = []
    try:
        lines = path(env, session_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def tokens(transcript: Path) -> dict[str, dict[str, int]]:
    """Tokens per model from a transcript's assistant messages, each message counted once."""
    seen: set[str] = set()
    out: dict[str, dict[str, int]] = {}
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        mid = str(message.get("id") or item.get("uuid") or "")
        if mid in seen:
            continue
        seen.add(mid)
        usage = message["usage"]
        row = out.setdefault(str(message.get("model") or "?"),
                             {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "messages": 0})
        row["input"] += int(usage.get("input_tokens") or 0)
        row["output"] += int(usage.get("output_tokens") or 0)
        row["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        row["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        row["messages"] += 1
    return out


def summary(env: Mapping[str, str], session_id: str) -> dict[str, Any]:
    """What a session's trace shows: records by tag, tools called, livespec's calls and its
    index events, notes recalled and context injected, locks' events, tokens per model."""
    rows = records(env, session_id)
    tags = Counter(r.get("tag") for r in rows)
    calls = [r for r in rows if str(r.get("tag", "")).startswith("tool:") and r.get("phase") == "PreToolUse"]
    if not calls:  # PreToolUse may be untraced on an older hooks file: count the answers
        calls = [r for r in rows if str(r.get("tag", "")).startswith("tool:")]
    tools = Counter(str(r["tag"])[5:] for r in calls)
    livespec = Counter(r["livespec_tool"] for r in calls if r.get("livespec_tool"))
    locks_events = [r for r in rows if str(r.get("tag", "")).startswith("locks:")]
    notes: list[Any] = []
    index: list[dict[str, Any]] = []
    for r in locks_events:
        data = r.get("data") or {}
        kind = str(r["tag"])[6:]
        if kind == "briefed" and data.get("notes"):
            notes.append({"task_id": r.get("task_id"), "notes": data.get("notes")})
        if "livespec" in kind or any(str(k).startswith("livespec") for k in data):
            index.append({"task_id": r.get("task_id"), "event": kind,
                          **{k: v for k, v in data.items() if str(k).startswith("livespec") or k == "index"}})
    transcript = path(env, session_id).with_suffix(".transcript.jsonl")
    return {
        "session_id": session_id, "records": len(rows), "by_tag": dict(tags.most_common()),
        "tools": dict(tools.most_common()), "livespec_calls": dict(livespec.most_common()),
        "livespec_events": index, "notes_briefed": notes,
        "injected": sum(n for t, n in tags.items() if str(t).startswith("inject:")),
        "locks_events": dict(Counter(str(r["tag"])[6:] for r in locks_events).most_common()),
        "tokens": tokens(transcript) if transcript.is_file() else {},
    }


#: How much of one call's input and output the board's terminal shows (`text.clip`): the trace keeps it whole.
SHOWN_INPUT = 400
SHOWN_OUTPUT = 1200


def _said(value: Any) -> str:
    """A tool's input or output as a terminal would show it: a command, a path, else its text."""
    if isinstance(value, Mapping):
        for key in ("command", "file_path", "path", "pattern", "url", "query", "stdout", "content", "output"):
            if isinstance(value.get(key), str) and value[key]:
                said = value[key]
                if key == "stdout" and isinstance(value.get("stderr"), str) and value["stderr"]:
                    said += "\n" + value["stderr"]
                return said
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, list):
        return "\n".join(_said(v) for v in value)
    return "" if value is None else str(value)


def worker_calls(env: Mapping[str, str], session_id: str | None, task_id: int, *, limit: int = 20) -> list[dict]:
    """A worker's last finished calls from its session's trace, for the board's terminal: when, the
    tool, its input and its output, masked as written and clipped for the view. Empty with the trace
    off or no session yet; it is the person's opt-in, read from their locks home only."""
    if not session_id or not enabled(env):
        return []
    from .text import clip

    out = []
    for r in records(env, session_id):
        if not str(r.get("tag", "")).startswith("tool:") or r.get("phase") != "PostToolUse":
            continue
        if r.get("worker_task") not in (None, task_id):
            continue
        out.append({"ts": r.get("ts"), "tool": str(r["tag"])[5:],
                    "input": clip(_said(r.get("input")), SHOWN_INPUT),
                    "output": clip(_said(r.get("response")), SHOWN_OUTPUT)})
    return out[-limit:]
