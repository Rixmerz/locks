"""Tool-call signatures, and the append-only log a hook writes them to.

This module is what runs on every tool call, so it imports only the standard
library pieces it needs — no SQLite, no orchestrator. A hook appends one JSON
line to `$LOCKS_HOME/tool-events/<day>.ndjson` and exits; the lines reach the
database when habits are mined (`locks.habits.load_events`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ALLOWED = re.compile(r"[^a-z0-9:._-]")
_TOKEN = re.compile(r"^(sk-|ghp_|gho_|github_pat_)|^[0-9a-fA-F]{20,}$|^[A-Za-z0-9+/]{20,}={0,2}$")
#: A secret's shape in a cleaned signature, or a run of letters and digits no program name has.
_LEAK = re.compile(r"(sk-|gh[pousr]_|github_pat_|akia)|[a-z0-9]{24,}")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Programs whose first argument is the real verb: `git commit`, `npm test`.
_TWO_WORD = frozenset({
    "git", "npm", "pnpm", "yarn", "bun", "uv", "cargo", "go", "make", "docker", "kubectl", "poetry",
    "pip", "npx", "dotnet", "gradle", "mvn", "deno", "terraform", "gh", "ruff", "biome",
})
_WRAPPERS = frozenset({"sudo", "time", "env", "nice", "exec", "command"})
_FILE_TOOLS = {"Read": "read", "Edit": "edit", "Write": "write", "MultiEdit": "edit", "NotebookEdit": "edit"}


def _clean(text: str) -> str:
    return _ALLOWED.sub("", text.lower())


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return command.split()


_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def _bash_signature(command: str) -> str:
    tokens = _tokens(command)
    i = 0
    while i < len(tokens) and (_ENV_ASSIGN.match(tokens[i]) or tokens[i] in _WRAPPERS):
        i += 1
    if i >= len(tokens) or _TOKEN.search(tokens[i]) or "://" in tokens[i]:
        return "bash:unknown"
    program, rest = Path(tokens[i]).name, tokens[i + 1:]
    if program in ("python", "python3") and len(rest) >= 2 and rest[0] == "-m":
        program, rest = rest[1], rest[2:]  # `python -m pytest` is pytest
    words = [program]
    if program in _TWO_WORD:
        for token in rest:
            if token in _SEPARATORS or "/" in token or "://" in token or _TOKEN.search(token):
                break
            if not token.startswith("-"):
                words.append(token)
                break
    found = _clean("-".join(words))
    # Checked again once cleaned: a token whose quotes `shlex` could not split (`"'ghp_…`) is a
    # secret's shape only after the cleaning strips them (found by `tests/test_fuzz.py`).
    return "bash:unknown" if not found or _LEAK.search(found) else "bash:" + found


#: A file's type as a signature keeps it: a real extension is short and plain. Anything after a
#: dot is not one (`notes.final draft`, a token after a dot): kept, it would carry the argument.
_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def _suffix(path: str) -> str:
    suffix = Path(path).suffix
    if not suffix:
        return "noext"
    return _clean(suffix) if _EXTENSION.match(suffix) and not _TOKEN.search(suffix[1:]) else "other"


def signature(tool: str, tool_input: Mapping[str, Any] | None) -> str:
    tool_input = tool_input or {}
    if tool == "Bash":
        return _bash_signature(str(tool_input.get("command") or ""))
    if tool in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return f"{_FILE_TOOLS[tool]}:{_suffix(path)}"
    if tool.startswith("mcp__"):
        return "mcp:" + _clean(tool[5:].replace("__", "."))
    return _clean(tool) or "unknown"


def arg_hash(tool_input: Mapping[str, Any] | None) -> str:
    raw = json.dumps(tool_input or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


DELEGATION_TOOLS = frozenset({"Agent", "Task"})
#: Not a step of anyone's work: the CLI's own answer to a JSON schema, made by
#: every classifier, dispatch and naming call locks asks Haiku.
NOT_STEPS = frozenset({"StructuredOutput"})
#: The events Claude Code fires instead of PostToolUse for a call that did not go through: one that
#: failed, and one a permission (a rule, the auto mode's classifier) refused before it ran. Each is a
#: failed call of the log; PostToolUse alone left them out, and the friction read 0 of 16 450.
FAILED = frozenset({"PostToolUseFailure", "PermissionDenied"})


def log_dir(env: Mapping[str, str]) -> Path:
    from locks import legacy

    if given := legacy.getenv(env, "LOCKS_HOME"):
        base = Path(given).expanduser()
    else:
        xdg = env.get("XDG_DATA_HOME", "")
        base = legacy.home(Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "share")
    return base / "tool-events"


def entry(event: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """One tool call as a record: never its arguments, contents or output."""
    response = event.get("tool_response")
    ok = event.get("hook_event_name") not in FAILED and not (
        isinstance(response, Mapping) and (response.get("is_error") or response.get("interrupted")))
    tool = str(event.get("tool_name") or "")
    task = env.get("LOCKS_WORKER_TASK")
    attempt = env.get("LOCKS_WORKER_ATTEMPT")
    return {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "session_id": event.get("session_id"),
        "task_id": int(task) if task and task.isdigit() else None,
        "attempt": int(attempt) if attempt and attempt.isdigit() else None,
        "tool": _clean(tool),
        "sig": signature(tool, event.get("tool_input")),
        "arg_hash": arg_hash(event.get("tool_input")),
        "ok": int(ok),
    }


def append(event: Mapping[str, Any], env: Mapping[str, str]) -> None:
    if event.get("tool_name") in NOT_STEPS:
        return
    directory = log_dir(env)
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry(event, env), separators=(",", ":")) + "\n"
    path = directory / f"{datetime.now(UTC):%Y-%m-%d}.ndjson"
    # One write of one line in append mode: concurrent hooks never interleave.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def note_error(env: Mapping[str, str]) -> None:
    """The same ledger the full hooks write to; SessionStart reports it."""
    try:
        import traceback

        path = log_dir(env).parent / "hook-errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')} PostToolUse\n"
                     f"{traceback.format_exc()}\n")
    except Exception:  # noqa: S110 - losing a note is acceptable, raising from here is not
        pass
