"""What a session established, kept through a compaction.

Claude Code's own summary is prose a model wrote from memory: paths, commands,
exact errors and the person's words come back paraphrased or not at all. Before
a compaction, locks reads the transcript itself and writes a digest of facts —
the person's requests verbatim, the files the session wrote, the commands that
failed and how, the files git shows changed — and the SessionStart that follows
the compaction puts it back in front of the model. No model call: the digest is
read, never recalled, so it cannot invent anything. Stdlib only; a hook runs it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from locks import legacy
from locks.notes import _SECRET
from locks.text import clip

#: The most the digest puts back into a session; sections are cut, in order of
#: worth, to fit.
BUDGET = 9000
#: The share of the budget the failed calls take at most; the rest is said by count.
FAILURES_SHARE = 5  # one fifth
#: The most of one request, one error or one command the digest keeps.
REQUEST_CHARS = 700
ERROR_CHARS = 200
COMMAND_CHARS = 160
MAX_LINE = 4 * 1024 * 1024
WRITERS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
#: Text the harness puts in a user turn that the person never typed.
NOT_TYPED = ("<command-", "<local-command", "<system-reminder>", "<task-notification>", "Caveat:",
             "[Request interrupted")
#: How a `/compact` locks asked for opens (`locks.compaction`): its marker
#: follows the command.
ASKED = "/compact [locks-compaction #"
#: A prompt a plugin submitted between turns, a bare slash command, or a
#: compaction locks asked for: in the person's place, not in their words.
NOT_TYPED_RE = re.compile(r"^(?:The [\w.:@-]+ plugin sent a message|/[\w:-]+$|" + re.escape(ASKED) + "|"
                          + re.escape(f"/compact [{legacy.OLD_COMPACTION} #") + ")")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def path(root: Path, session_id: str) -> Path | None:
    """Where a session's digest lives, or None for an id that is no file name."""
    return root / "compact" / f"{session_id}.md" if SAFE_ID.match(session_id) else None


def _entries(transcript: Path):
    with transcript.open("rb") as fh:
        for line in fh:
            if len(line) > MAX_LINE:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _typed(entry: dict[str, Any]) -> str | None:
    """The text a person typed in a user turn, or None."""
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = "\n".join(texts) if len(texts) == len(content) else None
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(NOT_TYPED) or NOT_TYPED_RE.match(text):
        return None
    return text


def _clip(text: str, limit: int) -> str:
    """One line, at most `limit`, with anything that looks like a secret masked:
    the digest is put in front of a model and kept on disk."""
    return clip(_SECRET.sub("[secret]", " ".join(text.split())), limit)


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def read(transcript: Path) -> dict[str, Any]:
    """The facts in a transcript: requests, written files, failed commands."""
    requests: list[str] = []
    written: dict[str, None] = {}
    calls: dict[str, tuple[str, str]] = {}
    failures: list[tuple[tuple[str, str], str]] = []
    for entry in _entries(transcript):
        kind = entry.get("type")
        content = (entry.get("message") or {}).get("content")
        if kind == "user":
            text = _typed(entry)
            if text and (not requests or requests[-1] != text):
                requests.append(text)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use":
                name, args = str(block.get("name", "")), block.get("input") or {}
                if name in WRITERS and isinstance(args.get("file_path") or args.get("notebook_path"), str):
                    written[args.get("file_path") or args.get("notebook_path")] = None
                what = args.get("command") if name == "Bash" else args.get("file_path") or args.get("pattern")
                calls[str(block.get("id"))] = (name, str(what or ""))
            elif kind == "user" and block.get("type") == "tool_result":
                call = calls.get(str(block.get("tool_use_id")), ("?", ""))
                if not block.get("is_error"):
                    # The same command passed later: the failure was fixed, it is not a fact to carry.
                    failures = [f for f in failures if f[0] != call] if call[1] else failures
                    continue
                error = _result_text(block)
                if "user doesn't want to proceed" in error or "rejected" in error[:80]:
                    continue  # a person's no is not a failure of the work
                failures.append((call, f"{call[0]} `{_clip(call[1], COMMAND_CHARS)}`: {_clip(error, ERROR_CHARS)}"))
    return {"requests": requests, "written": list(written), "failures": [text for _, text in failures]}


def _changed(cwd: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain"], capture_output=True, text=True,
                             timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line[3:] for line in out.stdout.splitlines() if len(line) > 3] if out.returncode == 0 else []


def _section(title: str, lines: list[str], room: int, *, newest_first: bool = False) -> str:
    """A section that fits in `room`: from the newest end when the old matter less; what it
    leaves out is said by count."""
    if not lines or room <= len(title) + 10:
        return ""
    picked: list[str] = []
    used = len(title) + 1
    order = list(reversed(lines)) if newest_first else lines
    for line in order:
        if used + len(line) + 1 > room:
            break
        picked.append(line)
        used += len(line) + 1
    if newest_first:
        picked.reverse()
    dropped = len(lines) - len(picked)
    if dropped and newest_first:
        picked.insert(0, f"- ({dropped} earlier left out)")
    elif dropped:
        picked.append(f"- (+{dropped} more)")
    return title + "\n" + "\n".join(picked)


def digest(facts: dict[str, Any], *, changed: list[str], tasks: list[dict[str, Any]], budget: int = BUDGET) -> str:
    """The digest text, within `budget`: what the person asked first, then what
    broke, then where the work is."""
    head = ("locks compact: facts read from this session's transcript and git before it was summarized. "
            "They are exact; where the summary disagrees, trust these.")
    sections = [
        ("The person's requests, verbatim, oldest first:",
         [f"- {_clip(r, REQUEST_CHARS)}" for r in facts["requests"]], True),
        ("locks tasks of this session:", [f"- #{t['id']} [{t['status']}] {t['title']}" for t in tasks], True),
        ("Calls that failed and never passed later (newest last):", [f"- {f}" for f in facts["failures"]], True),
        ("Files git shows changed now:", [f"- {c}" for c in changed], False),
        ("Files this session wrote:", [f"- {w}" for w in facts["written"]], True),
    ]
    out, room = [head], budget - len(head)
    # Requests get half the room at most and the failed calls a fifth of the budget, so the rest is
    # never crowded out.
    for index, (title, lines, newest) in enumerate(sections):
        share = room // 2 if index == 0 else room
        if title.startswith("Calls that failed"):
            share = min(share, budget // FAILURES_SHARE)
        text = _section(title, lines, share, newest_first=newest)
        if text:
            out.append(text)
            room -= len(text) + 2
    return "\n\n".join(out) if len(out) > 1 else ""


def keep(root: Path, session_id: str, transcript: Path, cwd: Path | None,
         tasks: list[dict[str, Any]]) -> Path | None:
    """PreCompact: write the session's digest. None when there is nothing to keep."""
    target = path(root, session_id)
    if target is None or not transcript.is_file():
        return None
    text = digest(read(transcript), changed=_changed(cwd) if cwd else [], tasks=tasks)
    if not text:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    return target


def recall(root: Path, session_id: str) -> str | None:
    """SessionStart after a compaction: the digest kept for this session."""
    target = path(root, session_id)
    if target is None or not target.is_file():
        return None
    return target.read_text(encoding="utf-8").strip() or None
