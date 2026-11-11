"""Adapters: neighbours the core adopts, instead of listing them as one more plugin.

A *capability* (see `locks.capabilities`) is a server the core hands to a
worker: the worker may call it, and the core knows nothing about what it says.
An *adapter* is a neighbour the core understands. It reads the neighbour's own
data with the standard library — no tokens, no MCP call — and uses it at the
three moments the core decides something:

- **before routing** — signals that move where a task starts (a symbol half the
  codebase calls; code a critical spec depends on);
- **before the worker starts** — a briefing, so the worker opens with the map
  instead of spending its first turns searching for it;
- **after a pass** — what the change touched that the worker did not, which
  goes in the report a person reads before merging.

And it hands the worker the neighbour's server, with a hint per kind of work
naming the calls that kind needs.

An adapter must say what it could not tell. *Absent* (not installed, repo not
indexed) and *unreadable* (installed, but its data could not be read) are
different: the first is a fact, the second is locks' or the neighbour's bug,
and neither may stop a run.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

PRESENT, ABSENT, UNREADABLE = "present", "absent", "unreadable"


@dataclass(frozen=True)
class Status:
    name: str
    state: str
    detail: str = ""
    version: str | None = None
    stale: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return self.state == PRESENT

    def line(self) -> str:
        parts = [f"{self.name}: {self.state}"]
        if self.version:
            parts.append(f"v{self.version}")
        if self.stale:
            parts.append("stale")
        if self.detail:
            parts.append(f"— {self.detail}")
        return " ".join(parts)


@dataclass(frozen=True)
class Briefing:
    """What an adapter tells the router and the worker before the first attempt."""

    lines: tuple[str, ...] = ()
    raise_rungs: int = 0
    critical: bool = False
    reasons: tuple[str, ...] = ()
    #: The symbols the request reaches (the ones it names and their callers), when an index
    #: could be read; None when none said it. A task's points weigh it (`locks.points`).
    reached: int | None = None


@dataclass(frozen=True)
class Dossier:
    """What an index says on a request, read by locks and put in the worker's brief as data."""

    text: str = ""
    #: The symbols and the specs the text carries, and what did not fit, said in the text by count.
    symbols: int = 0
    specs: int = 0
    omitted: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)


class Adapter(Protocol):
    name: str

    def inspect(self, repo_dir: Path) -> Status: ...

    def refresh(self, repo_dir: Path) -> str: ...

    def refresh_in_background(self, repo_dir: Path, log: Path) -> bool: ...

    def brief(self, text: str, repo_dir: Path, status: Status) -> Briefing: ...

    def assess(self, changed: Sequence[str], repo_dir: Path, status: Status) -> tuple[str, ...]: ...

    def server(self) -> Mapping[str, Any] | None: ...

    def hint(self, kind: str, repo_dir: Path, workdir: Path) -> str: ...


def default_adapters(env: Mapping[str, str] | None = None, *, livespec: bool | None = None) -> list[Adapter]:
    """The neighbours whose settings are on: livespec, then engram."""
    import os

    from locks import config
    from locks.adapters.engram import Engram
    from locks.adapters.livespec import Livespec

    env = os.environ if env is None else env
    out: list[Adapter] = []
    if config.enabled("livespec", env, livespec):
        out.append(Livespec())
    if config.enabled("engram", env):
        out.append(Engram(env))
    return out
