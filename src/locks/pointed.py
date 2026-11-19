"""The work a request points to in a document, not in its own words (#200).

"Implement phase 3 of docs/PLAN.md" is one line: its own text lists nothing, so the plan sizes it
small and nothing splits it, while the section it names may list a dozen items, and one worker on
all of them outgrows its turns. This reads that section: a Markdown file the request names that
exists in the repository, and a heading in it the request names by number after a word for a part
of a plan ("phase 3", "fase 2", "step 4", "milestone 1"), or by a heading's own words quoted in the
request. Its list items, up to the next heading of the same level or higher, are the work.

Read only, no model. Nothing is read when no section is named: a whole document is no task.
Stdlib.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: A document larger than this is not read: a plan is small, and the plan step must stay fast.
MAX_BYTES = 256 * 1024

_DOC = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:md|markdown|txt))(?![\w/])", re.IGNORECASE)
_PART = re.compile(r"\b(phase|fase|step|paso|stage|etapa|part|parte|milestone|hito|section|secci[oó]n|"
                   r"sprint|slice|iteration|iteraci[oó]n)\s*#?\s*(\d+)\b", re.IGNORECASE)
_QUOTED = re.compile(r"[\"'“‘`]([^\"'”’`\n]{3,80})[\"'”’`]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")


@dataclass(frozen=True)
class Pointed:
    """A section of a document a request points to, and its list items, verbatim."""

    doc: str
    heading: str
    items: tuple[str, ...]

    def line(self) -> str:
        return f"{len(self.items)} items in {self.doc} › {self.heading}"


def _named(heading: str, text: str) -> bool:
    """Whether the request names this heading: by a part and its number, or by its words quoted."""
    plain = heading.casefold()
    for match in _PART.finditer(text):
        number = match.group(2)
        if re.search(rf"\b{re.escape(match.group(1).casefold()[:4])}\w*\s*#?\s*{number}\b", plain):
            return True
        # A heading that numbers its parts in another language or by number alone ("3. Billing").
        if re.match(rf"^\s*(?:\w+\s+)?{number}\s*[:.)\-–—]", plain):
            return True
    return any(q.strip().casefold() in plain for q in _QUOTED.findall(text) if q.strip())


def section(text: str, where: str | Path | None) -> Pointed | None:
    """The section the request points to, with its list items, or None when it points to none
    this reads: no document it names exists under `where`, none is small enough, or no heading
    in it is named."""
    if where is None:
        return None
    try:
        root = Path(where).resolve()
    except OSError:
        return None
    for name in dict.fromkeys(m.group(1) for m in _DOC.finditer(text)):
        path = (root / name).resolve()
        try:
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            continue
        found = _in(lines, text)
        if found:
            heading, items = found
            return Pointed(name, heading, items)
    return None


def _in(lines: list[str], text: str) -> tuple[str, tuple[str, ...]] | None:
    fenced = False
    for n, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        heading = None if fenced else _HEADING.match(line)
        if not heading or not _named(heading.group(2), text):
            continue
        level, items, inner = len(heading.group(1)), [], False
        for rest in lines[n + 1:]:
            if rest.lstrip().startswith("```"):
                inner = not inner
                continue
            if inner:
                continue
            if (h := _HEADING.match(rest)) and len(h.group(1)) <= level:
                break
            # Top-level items only: a nested one is a detail of the item above it.
            if (item := _ITEM.match(rest)) and len(rest) - len(rest.lstrip()) < 2:
                items.append(item.group(1))
        if items:
            return heading.group(2), tuple(items)
    return None
