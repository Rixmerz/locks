"""TRACER as the way a project is built, not only documents a brief reads.

A project the person put on the method (`locks method tracer on`, kept in the store's meta, never in
a repository) is in one phase of it at a time, read from what `docs/tracer/` already holds, with no
model: the expectation contract, the complete spec, the empathy round, the critical flows and floors,
the design system, the slice plan, then the slices themselves. Each phase names the bundled workflow
that does it and who decides there. A slice's Definition of Ready is checked here too, in code, before
any worker is spent on it (`unready`). Nothing here writes: it reads files and says.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from locks import tracer
from locks.text import fold, words

#: The store's meta key, per repository key, that holds the method a person put a project on.
META = "method:"
NAME = "tracer"
OPENSPEC = "openspec"


@dataclass(frozen=True)
class Phase:
    id: str
    title: str
    #: The files under `docs/tracer/` (a trailing `/` is a folder with at least one file) the phase writes.
    needs: tuple[str, ...]
    #: The bundled workflow that does the phase.
    workflow: str
    #: Who decides there: the person's phases stay in their conversation.
    decides: str


PHASES = (
    Phase("contract", "Expectation contract: the MEV's metric, target and quality floor, and the audiences",
          ("contract.md", "audiences.md"), "tracer-contract", "person"),
    Phase("spec", "Complete spec, base round: every MEV requirement with scenarios, grouped by capability",
          ("capabilities.md",), "tracer-spec", "workers, checked"),
    Phase("empathy", "Empathy round: each capability read through its audience, expectations proposed",
          ("empathy/",), "tracer-empathy", "readers, then the person accepts"),
    Phase("flows", "Critical flows with WORKS and CORRECT halves, component floors, the anti-pattern catalogue",
          ("flows/", "floors.md", "anti-patterns.md"), "tracer-spec", "workers, checked"),
    Phase("design", "Design system from the catalogue and the product's identity",
          ("design-system.md", "identity.md"), "tracer-design", "person"),
    Phase("slices", "Slice plan: the skeleton, then slices of 1 to 3 days, one capability each",
          ("slices.md",), "tracer-slices", "plan, read blind"),
)

#: The status words a slice plan uses, folded, mapped to one of locks' four.
_STATUS = (("done", re.compile(r"\b(done|complete[d]?|closed|shipped|hecho|terminad[oa]|listo)\b")),
           ("in progress", re.compile(r"\b(in progress|doing|wip|en curso|en progreso)\b")),
           ("blocked", re.compile(r"\b(blocked|bloquead[oa])\b")),
           ("planned", re.compile(r"\b(planned|ready|todo|to do|pending|pendiente|planificad[oa])\b")))
_SEPARATOR = re.compile(r"^\|?[\s|:-]+\|?$")
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.+)$")


@dataclass
class Slice:
    name: str
    status: str


@dataclass
class State:
    on: bool
    present: bool
    #: Per phase, the files it should have written that are not there.
    missing: dict[str, list[str]] = field(default_factory=dict)
    slices: list[Slice] = field(default_factory=list)

    @property
    def phase(self) -> Phase | None:
        """The first phase whose files are not all there; None once every one is (the slices then)."""
        return next((p for p in PHASES if self.missing.get(p.id)), None)

    def next_slice(self) -> Slice | None:
        return next((s for s in self.slices if s.status in ("in progress", "planned")), None)

    def lines(self) -> list[str]:
        if not self.present:
            return [f"no `{tracer.FOLDER}/` here: the project has not started TRACER (its first phase is the "
                    f"expectation contract: `locks workflow run tracer-contract`)"]
        out = []
        for p in PHASES:
            gone = self.missing.get(p.id) or []
            mark = "done" if not gone else ("now" if p is self.phase else "to do")
            out.append(f"{mark:<6} {p.id:<9} {p.title}" + (f" (missing: {', '.join(gone)})" if gone else ""))
        if self.slices:
            counts = {k: sum(s.status == k for s in self.slices) for k in ("done", "in progress", "planned",
                                                                           "blocked")}
            out.append("slices: " + ", ".join(f"{n} {k}" for k, n in counts.items() if n))
        return out

    def advice(self) -> str:
        """The one line a build request hears on a project on the method: the phase it skips, or the slice."""
        if not self.present or self.phase is not None:
            phase = self.phase or PHASES[0]
            return (f"TRACER: this project is in its {phase.id} phase ({phase.title.split(':')[0].lower()}); "
                    f"building before it skips the method. Its workflow: `locks workflow run {phase.workflow}`"
                    + (" (the person decides there)" if phase.decides == "person" else ""))
        nxt = self.next_slice()
        if nxt is None:
            return "TRACER: every planned slice is done; what comes next is release and the metric's window"
        return f"TRACER: build work here goes slice by slice: next is \"{nxt.name}\" (`locks workflow run " \
               f"tracer-slice --input slice=\"{nxt.name}\"`)"


def on(store: Any, key: str | None) -> bool:
    return bool(key) and store.meta(META + str(key)) == NAME


def set_on(store: Any, key: str | None, value: bool) -> None:
    store.set_meta(META + str(key), NAME if value else "off")


def _has(folder: Path, need: str) -> bool:
    if need.endswith("/"):
        sub = folder / need.rstrip("/")
        return sub.is_dir() and any(p.is_file() for p in sub.iterdir())
    path = folder / need
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _status(text: str) -> str:
    folded = fold(text)
    return next((name for name, rx in _STATUS if rx.search(folded)), "planned")


def slices(repo_dir: Path) -> list[Slice]:
    """The slices `slices.md` plans: a table's rows (a status cell read by its words) or checkbox entries."""
    try:
        body = (repo_dir / tracer.FOLDER / "slices.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Slice] = []
    header = True
    for raw in body.splitlines():
        line = raw.strip()
        if box := _CHECKBOX.match(raw):
            out.append(Slice(box.group(2).strip(), "done" if box.group(1) in "xX" else _status(box.group(2))))
            continue
        if not line.startswith("|"):
            header = True
            continue
        if _SEPARATOR.match(line):
            header = False
            continue
        if header:
            continue
        cells = [c.strip().replace("*", "").replace("`", "") for c in line.strip("|").split("|")]
        name = next((c for c in cells if words(c) and not c.strip().isdigit()), "")
        if name:
            out.append(Slice(name, _status(" ".join(cells[1:]) if len(cells) > 1 else "")))
    return out


def state(repo_dir: Path, *, is_on: bool = True) -> State:
    folder = repo_dir / tracer.FOLDER
    found = State(on=is_on, present=folder.is_dir())
    if found.present:
        found.missing = {p.id: [n for n in p.needs if not _has(folder, n)] for p in PHASES}
        found.slices = slices(repo_dir)
    else:
        found.missing = {p.id: list(p.needs) for p in PHASES}
    return found


_ESCAPED = re.compile(r"escaped expectation", re.IGNORECASE)
#: How many of a repository's latest tasks the record reads.
RECORD_TASKS = 500


@dataclass
class Record:
    """What the slices built here show, read from their verification steps' endings."""
    verified: int = 0
    clean: int = 0
    escaped: int = 0
    examples: list[str] = field(default_factory=list)

    def line(self) -> str | None:
        if not self.verified:
            return None
        return (f"slices verified: {self.verified} ({self.clean} clean); escaped expectations: {self.escaped}"
                + (" (the verifier found what no CORRECT line, floor or catalogue entry covered: "
                   "each one is a line the spec gains)" if self.escaped else ""))


def record(store: Any, key: str | None) -> Record:
    """The verification steps of `tracer-slice` runs here that ended, their findings counted: a finding
    the verifier calls an escaped expectation is one the method's spec did not cover. Read only."""
    out = Record()
    for task in store.list_tasks(repo=key, limit=RECORD_TASKS):
        if task.get("workflow_step") != "verify" or not str(task.get("title") or "").startswith("[tracer-slice#"):
            continue
        ended = store.final_event(int(task["id"]))
        if ended is None:
            continue
        found = (ended.get("data") or {}).get("findings") or []
        out.verified += 1
        out.clean += not found
        for finding in found:
            text = " ".join(str(finding.get(k) or "") for k in ("what", "where", "cause")) \
                if isinstance(finding, dict) else str(finding)
            if _ESCAPED.search(text):
                out.escaped += 1
                if len(out.examples) < 3:
                    out.examples.append(text.strip())
    return out


def _specs(repo_dir: Path) -> bool:
    spec = repo_dir / OPENSPEC
    return spec.is_dir() and any(spec.rglob("*.md"))


def unready(repo_dir: Path, text: str, *, has: Callable[[Path, str], bool] = _has) -> list[str]:
    """What a slice's Definition of Ready lacks, read from the files alone: requirements with scenarios,
    the empathy round, one capability of the map it lands on, its audience, at least one critical flow and
    the component floors. An empty list is ready; a slice only one rule cannot tell (no map to land on) is
    not held for it. Never a model's word: a slice that reads ready here may still be the worker's to stop."""
    folder = repo_dir / tracer.FOLDER
    missing = []
    if not _specs(repo_dir):
        missing.append(f"its requirements with scenarios (`{OPENSPEC}/` holds none)")
    if not has(folder, "empathy/"):
        missing.append("the empathy round (`docs/tracer/empathy/` is empty)")
    names = tracer.capability_names(repo_dir)
    if not names:
        missing.append("the capability map it lands on (`docs/tracer/capabilities.md`)")
    else:
        asked = set(words(text))
        if not any(tracer._covered(words(n), asked, 1) for n in names):
            missing.append("which capability of `docs/tracer/capabilities.md` it lands on (the slice names none)")
    if not has(folder, "audiences.md"):
        missing.append("its audience profile (`docs/tracer/audiences.md`)")
    if not tracer.flows_for(repo_dir, text):
        missing.append("a critical flow it touches, with WORKS and CORRECT (`docs/tracer/flows/`)")
    if not has(folder, "floors.md"):
        missing.append("the component floors it inherits (`docs/tracer/floors.md`)")
    return missing
