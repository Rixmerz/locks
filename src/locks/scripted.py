"""Whether the Claude Code session a hook runs for is a program's, not a person's.

A script that runs `claude -p` (a batch of structured questions, an experiment, a pipeline) holds no
conversation: locks' notes, its route advice and the endings of work are context it never asked for, and
they make its answers depend on what locks' memory held at that moment. Its own text is no conversation
for the scribe or the notes either, and it must never take a relay or the orchestrator's role.

Environment variables cannot tell it: a `claude -p` started from a session's shell inherits that
session's. The session's own process can: the nearest `claude` above the hook, its arguments and
what its input is. It is a program's when it runs in print mode (`-p`, `--print`), its input is no
stream of messages (an app holding a conversation over `--input-format stream-json`), and either it
asks for structured output (`--json-schema`), its output is read by a program (`--output-format json`
or `stream-json`), or its input is no terminal. A person's `claude -p "…"` typed in a terminal keeps
the hooks. What cannot be read (no `claude` found, a process that cannot be read) is no program's:
the hooks go on as before. `LOCKS_SCRIPTED_CHECK=off` skips the look, as the tests do.
Standard library only, imports nothing of locks.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

CHECK = "LOCKS_SCRIPTED_CHECK"
#: How many processes above the hook are read before giving up: the hook's shell, the launcher, an
#: interpreter, `claude` itself.
MAX_DEPTH = 8
_PRINT = frozenset({"-p", "--print"})
_PROGRAM_OUTPUT = frozenset({"json", "stream-json"})

#: (parent pid, argv, whether its input is a terminal or None when that cannot be read).
Process = tuple[int, list[str], bool | None]


def _option(argv: Sequence[str], name: str) -> str | None:
    """The value of `--name value` or `--name=value`, the last one given."""
    found = None
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            found = argv[i + 1]
        elif arg.startswith(name + "="):
            found = arg.split("=", 1)[1]
    return found


def why(argv: Sequence[str], tty: bool | None) -> str | None:
    """Why the session `argv` runs is a program's, or None when it may be a person's."""
    if not _PRINT.intersection(argv) or _option(argv, "--input-format") == "stream-json":
        return None
    if any(a == "--json-schema" or a.startswith("--json-schema=") for a in argv):
        return "it asks for structured output"
    if _option(argv, "--output-format") in _PROGRAM_OUTPUT:
        return "a program reads its output"
    if tty is False:
        return "its input is no terminal"
    return None


def is_claude(argv: Sequence[str]) -> bool:
    """`claude` itself, or node running Claude Code's CLI."""
    if not argv:
        return False
    if os.path.basename(argv[0]) in ("claude", "claude.exe"):
        return True
    return len(argv) > 1 and (os.path.basename(argv[1]) == "claude"
                              or ("claude-code" in argv[1] and argv[1].endswith("cli.js")))


def _proc(pid: int) -> Process | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None
    try:
        stdin = os.readlink(f"/proc/{pid}/fd/0")
        tty: bool | None = stdin.startswith(("/dev/pts/", "/dev/tty"))
    except OSError:
        tty = None
    return ppid, argv, tty


def _ps(pid: int) -> Process | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2, check=False).stdout.strip()
        ppid, _, command = out.partition(" ")
        return int(ppid), command.split(), None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_process(pid: int) -> Process | None:
    return _proc(pid) if sys.platform.startswith("linux") else _ps(pid)


def session_process(start: int | None = None,
                    read: Callable[[int], Process | None] = read_process) -> tuple[list[str], bool | None] | None:
    """The nearest `claude` above this process: its argv and whether its input is a terminal."""
    pid = os.getppid() if start is None else start
    for _ in range(MAX_DEPTH):
        if pid <= 1:
            return None
        found = read(pid)
        if found is None:
            return None
        ppid, argv, tty = found
        if is_claude(argv):
            return argv, tty
        pid = ppid
    return None


def scripted(env: Mapping[str, str], start: int | None = None,
             read: Callable[[int], Process | None] = read_process) -> str | None:
    """Why the session this hook runs for is a program's, or None. Never raises."""
    if "off" in (str(env.get(CHECK, "")).lower(), os.environ.get(CHECK, "").lower()):
        return None
    try:
        found = session_process(start, read)
    except Exception:  # a look that fails is no program's session
        return None
    return why(*found) if found else None
