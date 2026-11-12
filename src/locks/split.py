"""A request that is several tasks, as the pieces it splits into (`locks split`).

A build too big for one worker's turns costs the most as one task: every turn re-reads
everything read so far, and the climb after it (more turns, a stronger model) pays that
again. `locks run` refuses one before it starts (`route.split_first`), and a run stops one
that outgrew its turns as several pieces (cause `split`). Either way the orchestrator gets
the pieces from here instead of inventing them:

- a request that lists its items is split by them, with no model, each item a piece, the
  folders it names as its surfaces;
- prose is split by Haiku, read strictly as a plan's parts are (`launch.read_parts`: ids,
  prompts, surfaces no other piece reaches, `after` naming an earlier one); an answer that
  does not read gives no pieces, never a guess.

Nothing is queued unless asked (`--queue`): then each piece starts on what the task it
splits kept, the work queued after that task waits for the last piece, and the task leaves
the board, dismissed with the pieces' numbers.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from locks import criteria, launch, points, route, surfaces
from locks.text import clip

#: The most pieces a split proposes: past this, a feature workflow plans it.
MAX_PIECES = 6
#: The most of a repository's layout Haiku is shown: its top-level folders and their files.
LAYOUT_LINES = 60

Ask = Callable[..., tuple[dict | None, float, str]]

SYSTEM = (
    "You split one software request into the fewest independent tasks that each fit one worker. "
    f"Give 2 to {MAX_PIECES} pieces. Each piece has an id (lowercase letters, digits, '-'), a prompt "
    "written so a stranger could do it (what to build or change and how it is checked, never how), and "
    "the surfaces it may change: folders or globs no other piece reaches. A piece that needs another's "
    "work names it in `after` (one piece listed before it). Give the contract the pieces share: names, "
    "types and interfaces. Split by what can be built and checked apart, never by role: a piece writes "
    "its own tests. When work was already done, say in each prompt that part of it may be done already."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "prompt": {"type": "string"},
            "surfaces": {"type": "array", "items": {"type": "string"}},
            "after": {"type": "string"}}, "required": ["id", "prompt", "surfaces"]}},
        "contract": {"type": "string"},
    },
    "required": ["parts"],
}


@dataclass(frozen=True)
class Proposal:
    """The pieces a request splits into, and how they were read; `problem` when none could be."""

    parts: tuple[dict[str, Any], ...] = ()
    contract: str = ""
    source: str = ""
    problem: str = ""
    cost_usd: float = 0.0
    done: tuple[str, ...] = field(default_factory=tuple)

    def data(self) -> dict[str, Any]:
        return {"parts": list(self.parts), "contract": self.contract, "source": self.source,
                "problem": self.problem, "cost_usd": round(self.cost_usd, 4), "done": list(self.done)}


def _head(text: str, items: Sequence[str]) -> str:
    """What the request says before its list: the context every piece keeps."""
    first = text.find(items[0]) if items else -1
    head = text[:first] if first > 0 else ""
    lines = [line for line in head.splitlines() if line.strip() and not line.strip().startswith(("-", "*"))]
    return " ".join(" ".join(lines).split())


def _exists(where: Path | None, surface: str) -> bool:
    """Whether a surface names something in the work directory: the path itself, or for a glob
    its literal folder (the part before the first wildcard). Unknown directory: kept."""
    if where is None:
        return True
    literal = PurePosixPath(surface).parts
    keep = []
    for part in literal:
        if any(c in part for c in "*?["):
            break
        keep.append(part)
    return (where / Path(*keep)).exists() if keep else True


def _surfaces(item: str, where: Path | None = None) -> tuple[list[str], list[str]]:
    """The folders a piece's item names, as surfaces (a file's folder, a folder itself), and the
    ones left out because the repository does not have that path (`_exists`)."""
    out, absent = [], []
    for path in points.named(item):
        p = PurePosixPath(path.strip("/"))
        folder = p if not p.suffix else p.parent
        if str(folder) in (".", ""):
            continue
        (out if _exists(where, f"{folder}/**") else absent).append(f"{folder}/**")
    return sorted(dict.fromkeys(out)), sorted(dict.fromkeys(absent))


def same_text(texts: Sequence[str]) -> bool:
    """Whether pieces are one task asked several times: every text is the same once spaces are
    folded. Their surfaces do not make them different work."""
    return len({" ".join(str(t).split()) for t in texts}) < 2


def by_items(text: str, done: Sequence[str] = (), where: Path | None = None) -> Proposal:
    """A request that lists its items, split by them with no model: each item a piece. `where` is
    the work directory: a surface derived from an item is kept only when it exists there."""
    listed = criteria.items(text)
    items = route.pieces_of(text)
    if len(items) < 2:
        return Proposal(problem="it lists fewer than two items")
    if route.one_file(text, items):
        return Proposal(problem=f"its {len(items)} listed items are all changes to one file or document: it "
                                "is one task", source="whole")
    if len(items) > MAX_PIECES:
        return Proposal(problem=f"it lists {len(items)} items, more than {MAX_PIECES} pieces: plan it as a "
                                "feature (`locks workflow run feature`), whose plan groups them")
    head = _head(text, listed)
    # rules the request lists (limits, tests, what not to do) are context for every piece
    if rules := [item for item in listed if route.is_rule(item)]:
        head = (head + "\n\n" if head else "") + "Rules of the whole request, for this piece too:\n" + "\n".join(
            f"- {rule}" for rule in rules)
    already = (" Part of this request was already worked on, on the branch this task starts from: finish what "
               "is left of this piece.") if done else ""
    derived = [_surfaces(item, where) for item in items]
    found = [mine for mine, _ in derived]
    # Pieces whose folders collide would be held one behind the other by their surfaces alone, even when
    # they name different files (the field reports' sequential autosplit): those go without surfaces, and
    # the dispatcher judges them by what each names (`dispatch.named_surfaces`).
    crowded = {n for n, mine in enumerate(found) if mine and any(
        other and not surfaces.disjoint(mine, other) for m, other in enumerate(found) if m != n)}
    parts = tuple({"id": f"p{n}", "prompt": (f"{head}\n\n" if head else "") + f"This task is one piece of that "
                   f"request: {item}{already}", "surfaces": [] if n - 1 in crowded else mine,
                   **({"absent": gone} if gone else {})}
                  for n, (item, mine, (_, gone)) in enumerate(zip(items, found, derived, strict=True), 1))
    if same_text(items):
        return Proposal(problem=f"its {len(items)} pieces are the same text but for their surfaces: it is one "
                                "task", source="whole")
    return Proposal(parts=parts, source="items", done=tuple(done))


def layout(where: Path | None, *, run: Callable[..., Any] = subprocess.run) -> list[str]:
    """The repository's top-level folders, each with how many tracked files it holds."""
    if where is None:
        return []
    try:
        out = run(["git", "-C", str(where), "ls-files"], capture_output=True, text=True, timeout=30,
                  check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    counts: dict[str, int] = {}
    for line in out.splitlines():
        top = line.split("/", 1)[0] if "/" in line else "."
        counts[top] = counts.get(top, 0) + 1
    return [f"{top}/ ({n} files)" if top != "." else f"(root: {n} files)" for top, n in sorted(counts.items())][
        :LAYOUT_LINES]


def by_model(text: str, where: Path | None, *, done: Sequence[str] = (), ask: Ask | None = None) -> Proposal:
    """Prose split by Haiku, its answer read strictly (`launch.read_parts`)."""
    if ask is None:
        from locks.classify import ask_haiku as ask
    question = "\n".join([
        "The request:", text, "",
        "The repository's layout:", *(layout(where) or ["(unknown)"]),
        *(["", "Files the work already changed:", *done[:40]] if done else []),
    ])
    answer, cost, why = ask(SYSTEM, SCHEMA, question)
    if answer is None:
        return Proposal(problem=f"the model gave no split: {why}", source="haiku", cost_usd=cost)
    parts, contract, problem = launch.read_parts(answer)
    if problem:
        # Its form failed, not the split: it is told what, once, as a worker is.
        answer, more, why = ask(SYSTEM, SCHEMA, f"{question}\n\nYour last split could not be used: {problem}. "
                                "Give surfaces no other piece can reach (each its own folder or files).")
        cost += more
        parts, contract, problem = launch.read_parts(answer) if answer is not None else ((), "", why)
    if problem:
        return Proposal(problem=f"the model's split does not read: {problem}", source="haiku", cost_usd=cost)
    if len(parts) < 2 or same_text([str(p["prompt"]) for p in parts]):
        return Proposal(problem="the model found one piece: it is one task", source="haiku", cost_usd=cost)
    return Proposal(parts=parts, contract=contract, source="haiku", cost_usd=cost, done=tuple(done))


def propose(text: str, where: Path | None, *, done: Sequence[str] = (), ask: Ask | None = None) -> Proposal:
    """The pieces a request splits into: by its listed items when it has them, else by Haiku."""
    listed = by_items(text, done, where)
    if listed.parts or listed.source == "whole" or len(criteria.items(text)) > MAX_PIECES:
        return listed
    return by_model(text, where, done=done, ask=ask)


def siblings(store: Any, number: int) -> list[int]:
    """The pieces of the split a task belongs to, oldest first: `number` is a piece (its
    `split_from` event names the task it was split from, and the group it was queued in) or
    the task they were split from. Empty when it is neither."""
    events = store.events_of_kinds(["split_from"])
    mine = next((e for e in events if e["task_id"] == number), None)
    if mine is not None:
        data = mine["data"]
        key = ("group", data["group"]) if data.get("group") is not None else ("task", data.get("task"))
        if key[1] is None:
            return [number]
    else:
        key = ("task", number)
    return sorted({e["task_id"] for e in events if e["data"].get(key[0]) == key[1]})


def brief(part: Mapping[str, Any], contract: str) -> str:
    """A piece's task text: its prompt, and the contract it shares with the others, quoted."""
    if not contract:
        return str(part["prompt"])
    quoted = "\n".join(f"> {line}" for line in contract.splitlines())
    return (f"{part['prompt']}\n\nWhat the pieces of this request share (context, as agreed when it was "
            f"split):\n{quoted}")


def lines(proposal: Proposal) -> list[str]:
    """The proposal as the orchestrator reads it."""
    if proposal.problem:
        return [f"no split: {proposal.problem}"]
    said = [f"{len(proposal.parts)} pieces ({'by its listed items' if proposal.source == 'items' else 'by Haiku'}"
            + (f", ${proposal.cost_usd:.3f}" if proposal.cost_usd else "") + "):"]
    for part in proposal.parts:
        where = ", ".join(part.get("surfaces") or ()) or "no surfaces: the dispatcher decides what runs beside it"
        if part.get("absent"):
            where += f" ({clip(', '.join(part['absent']), 80)} left out: not in the repository)"
        after = f", after {part['after']}" if part.get("after") else ""
        said.append(f"  {part['id']}{after} [{where}]: {' '.join(str(part['prompt']).split())[:300]}")
    if proposal.contract:
        said.append("  shared: " + " ".join(proposal.contract.split())[:300])
    return said


def as_json(proposal: Proposal) -> str:
    return json.dumps(proposal.data(), ensure_ascii=False)
