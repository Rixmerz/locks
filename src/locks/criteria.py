"""What was asked, kept as asked: a request's items as numbered criteria.

A request that lists what it wants (bullets, numbered lines) becomes criteria
`S1..Sn`, each the person's own words, verbatim. Every worker and every check
reads the same words: a paraphrase loses the detail that decides a check. A
worker that claims a pass says, criterion by criterion, whether each is met
and what shows it; a pass that leaves one out, or says one is not met, is not
a pass.

A request in prose has no criteria: splitting sentences would invent
structure the person did not write. Items the request itself puts out of scope
— listed under a line like "Part B (do not touch):" or "Fuera de alcance:", or
marked so on their own line — are no criteria: a pass is never asked to meet
what the person said not to do. The brief names them as left out.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)]|\[[ xX]\])\s+(?P<text>\S.*)$")
ITEM_CHARS = 400
#: Words that put what follows out of this task's scope.
_OUT_OF_SCOPE = re.compile(
    r"\b(no (?:toques|tocar|modifiques|cambies|implementes|hagas)|fuera de(?:l)? alcance|out of scope|"
    r"do not (?:touch|implement|change|do)|don'?t (?:touch|implement|change|do)|not (?:part of|in scope)|"
    r"no (?:es|forma) parte|para (?:despu[eé]s|m[aá]s adelante)|en otra tarea|in another task|"
    r"next (?:task|phase))\b", re.IGNORECASE)
#: On an item of its own, only words about scope: "- do not touch the header" is a
#: requirement to keep, not an item out of the task.
_ITEM_OUT_OF_SCOPE = re.compile(
    r"\b(fuera de(?:l)? alcance|out of scope|not (?:part of|in scope)|no (?:es|forma) parte|"
    r"para (?:despu[eé]s|m[aá]s adelante)|en otra tarea|in another task|next (?:task|phase))\b", re.IGNORECASE)


def _split(text: str) -> tuple[list[str], list[str]]:
    """(in scope, out of scope): the listed items, the ones under a line that puts them
    out of scope (a line ending in ':') or marked so themselves set apart. A quoted line (it starts
    with `>`) is context a step was handed, never the step's own words: it is no item and no header."""
    kept, left = [], []
    skipping = False
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        match = _ITEM.match(line)
        if match is None:
            if line.strip():
                skipping = line.rstrip().endswith(":") and bool(_OUT_OF_SCOPE.search(line))
            continue
        item = match.group("text").strip()[:ITEM_CHARS]
        (left if skipping or _ITEM_OUT_OF_SCOPE.search(item) else kept).append(item)
    return kept, left


def items(text: str) -> list[str]:
    """The request's listed items in scope, verbatim, when it lists two or more."""
    found, _ = _split(text)
    return found if len(found) >= 2 else []


def left_out(text: str) -> list[str]:
    """The listed items the request itself puts out of scope."""
    return _split(text)[1]


def ids(text: str) -> tuple[str, ...]:
    return tuple(f"S{i}" for i, _ in enumerate(items(text), 1))


def brief(text: str) -> str:
    """The criteria for the brief, or "" when the request lists none."""
    found = items(text)
    outside = left_out(text)
    parts = []
    if found:
        lines = "\n".join(f"S{i}: {item}" for i, item in enumerate(found, 1))
        parts.append("The request's criteria, in the person's words. Check each one; a pass says for every one "
                     "whether it is met and what shows it, in `criteria`:\n" + lines)
    if outside:
        parts.append("Out of scope, as the request says: do not do these, and they are no criteria of yours:\n"
                     + "\n".join(f"- {item}" for item in outside))
    return "\n\n".join(parts)


def read(block: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The criteria a result block reports: id, met, evidence; strings and booleans only."""
    raw = block.get("criteria")
    if not isinstance(raw, list):
        return ()
    out = []
    for c in raw:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("met"), bool):
            out.append({"id": c["id"].strip().upper(), "met": c["met"],
                        "evidence": str(c.get("evidence") or "")[:500]})
    return tuple(out)


def unmet(expected: Sequence[str], reported: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """The criteria a pass leaves out, and the ones it says are not met."""
    said = {c["id"]: c["met"] for c in reported}
    return [i for i in expected if i not in said], [i for i in expected if said.get(i) is False]
