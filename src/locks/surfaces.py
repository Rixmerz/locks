"""Where a task may write: its edit surfaces, paths and globs a person gives it
(`--surface src/api/**`) or a workflow step names.

A worker is told its surfaces; what it changed is read from git after each
attempt, and a change outside them is the attempt's failure, undone in the
task's worktree (never in a person's checkout). So the bound is enforced, not
only asked for. Surfaces narrow; they grant nothing, so a definition a
repository commits may carry them.

Two tasks whose surfaces cannot overlap may run side by side in one
repository without asking anyone. When it cannot be told (a glob this cannot
read, a task without surfaces), they may overlap, and the dispatcher's own
judgment decides.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath

_GLOB = re.compile(r"[*?\[]")
MAX_SURFACES = 20


class SurfaceError(ValueError):
    pass


def check(values: Iterable[str]) -> tuple[str, ...]:
    """Surfaces as given, each relative and inside the project."""
    out = []
    for raw in values:
        value = str(raw).strip()
        if value in (".", "./"):
            value = "**"
        while value.startswith("./"):
            value = value[2:]
        if not value:
            continue
        if value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise SurfaceError(f"a surface stays inside the project: {raw!r}")
        out.append(value.rstrip("/") + ("/**" if value.endswith("/") else ""))
    if len(out) > MAX_SURFACES:
        raise SurfaceError(f"at most {MAX_SURFACES} surfaces")
    return tuple(dict.fromkeys(out))


def _regex(surface: str) -> re.Pattern[str]:
    """A surface as a pattern: `**` crosses directories, `*` and `?` do not, and a
    literal path holds itself and everything under it."""
    out, i = "", 0
    while i < len(surface):
        if surface.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif surface.startswith("**", i):
            out, i = out + ".*", i + 2
        elif surface[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif surface[i] == "?":
            out, i = out + "[^/]", i + 1
        elif surface[i] == "[":
            end = surface.find("]", i)
            if end == -1:
                out, i = out + re.escape("["), i + 1
            else:
                out, i = out + surface[i:end + 1], end + 1
        else:
            out, i = out + re.escape(surface[i]), i + 1
    return re.compile(out + ("(?:/.*)?" if not _GLOB.search(surface) else "") + r"\Z")


def _norm(surface: str) -> str:
    while surface.startswith("./"):
        surface = surface[2:]
    return surface.rstrip("/") or surface


#: Folders a tool installs: never a surface to offer (issue #123).
DEPENDENCY_DIRS = frozenset({"node_modules", "vendor", ".venv", "venv", "__pycache__"})


def dependency(path: str) -> bool:
    """Whether a path lies in a folder a package manager fills."""
    return any(part in DEPENDENCY_DIRS for part in path.strip("/").split("/"))


def allows(surfaces: Sequence[str], path: str) -> bool:
    """Whether a changed path is inside the surfaces. A path named from a folder of several
    repositories (`api/src/x.py`) is also read from inside its repository (`src/x.py`): surfaces
    are written from where the work is, the repository's root or the folder (issue #123)."""
    forms = [path]
    if "/" in path:
        forms.append(path.split("/", 1)[1])
    return any(_regex(_norm(s)).match(f) for s in surfaces for f in forms)


def outside(surfaces: Sequence[str], changed: Iterable[str]) -> list[str]:
    return [p for p in changed if not allows(surfaces, p)] if surfaces else []


#: The file of a failing test line (`launch.check_failures`): pytest's `FAILED tests/a.py::test_x`
#: and `ERROR tests/a.py`, jest's and vitest's `FAIL src/a.test.ts > suite > test`.
_TEST_FILE = re.compile(r"^(?:FAILED|ERROR|FAIL)\s+(\S+?\.\w+)(?:::|\s|$)")


def failing_outside(surfaces: Sequence[str], failing: Iterable[str]) -> list[str]:
    """The files of the failing tests of a check when every one of them is outside `surfaces`;
    empty when there are no surfaces or no failing line, when any line cannot be read to a file,
    or when any failing test is inside them (the work's own tests: a climb may fix them)."""
    files: list[str] = []
    for line in sorted(failing):
        hit = _TEST_FILE.match(line)
        if hit is None:
            return []
        path = hit.group(1)
        while path.startswith("./"):
            path = path[2:]
        if allows(surfaces, path):
            return []
        files.append(path)
    return list(dict.fromkeys(files)) if surfaces else []


def _prefix(surface: str) -> str:
    """The literal directory a surface starts with, before any glob."""
    head = _GLOB.split(surface, 1)[0]
    return head if _GLOB.search(surface) is None else head.rsplit("/", 1)[0] if "/" in head else ""


def disjoint(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether two sets of surfaces can never touch the same file. When in doubt, no."""
    if not a or not b:
        return False
    for x in a:
        for y in b:
            px, py = _prefix(x), _prefix(y)
            if not px or not py:
                return False  # a glob from the top could reach anywhere
            if (px == py or px.startswith(py + "/") or py.startswith(px + "/")) and _may_touch(x, y):
                return False
    return True


def _segment_apart(a: str, b: str) -> bool:
    """Whether no name can match both segments: read from what each holds before its first glob
    character and after its last one. `test_rule_*.py` and `test_receipt.py` start apart; in doubt, no."""
    if _GLOB.search(a) is None and _GLOB.search(b) is None:
        return a != b
    if "[" in a or "[" in b:
        return False  # a character class is not read here
    head_a, head_b = _GLOB.split(a, 1)[0], _GLOB.split(b, 1)[0]
    tail_a, tail_b = _GLOB.split(a)[-1], _GLOB.split(b)[-1]
    if _GLOB.search(a) is None:
        head_a = tail_a = a
    if _GLOB.search(b) is None:
        head_b = tail_b = b
    heads = head_a.startswith(head_b) or head_b.startswith(head_a)
    tails = tail_a.endswith(tail_b) or tail_b.endswith(tail_a)
    return not (heads and tails)


def _may_touch(x: str, y: str) -> bool:
    """Whether two surfaces under one folder may hold the same file, segment by segment: a literal
    path holds everything under it, `*` and `?` stay inside a segment, and `**` is never read apart.
    A plan's `tests/test_rule_*.py` beside `tests/test_receipt.py` was refused as overlapping."""
    xs, ys = _norm(x).split("/"), _norm(y).split("/")
    if any("**" in s for s in (*xs, *ys)):
        return True
    # A glob names files of exactly its depth; a literal path may be a folder holding deeper ones.
    if len(xs) != len(ys):
        shorter = ys if len(xs) > len(ys) else xs
        if _GLOB.search("/".join(shorter)) is not None:
            return False  # the shorter one is a glob: it reaches nothing as deep as the longer
    return not any(_segment_apart(a, b) for a, b in zip(xs, ys, strict=False))


def overlapping(groups: Mapping[str, Sequence[str]]) -> list[str]:
    """Each pair of surfaces of two groups (a plan's parts) that may touch the same file,
    as `api's src/** and web's src/web/**`; empty when every group is disjoint from the rest."""
    names, out = list(groups), []
    for n, a in enumerate(names):
        for b in names[n + 1:]:
            out += [f"{a}'s {x} and {b}'s {y}" for x in groups[a] for y in groups[b] if not disjoint([x], [y])]
    return out


def brief(surfaces: Sequence[str]) -> str:
    if not surfaces:
        return ""
    return ("Change only files inside these surfaces: " + ", ".join(surfaces) + ". A change outside them is "
            "undone and fails the attempt; if the task needs one, say so and stop.")
