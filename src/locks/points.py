"""How big a task is, on the scale people give tickets: 1, 2, 3, 5, 8, 13.

Two readings, never by a model. The **estimate** is made when the task is
planned, from what the plan already knows: how many criteria the request lists,
its kind, its risk tier and the areas that raised it, the files and surfaces it
names, how many symbols livespec says it reaches (when the index is readable),
and its length, the weakest signal. The **realized** size is read when it ends,
from what git and the attempts measured: files and lines changed, turns, wall
time, attempts. Both are on the same scale, so the two can be compared.

Each signal adds a weight by named thresholds, and the sum lands on the scale;
every weight that counted is a reason, so "5: 4 criteria, touches schema, risk
high" says how it was reached. A standard per points band (`locks.standards`)
is what the score is for: where a task of that size starts.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from locks import criteria as criteria_mod
from locks import risk as risk_mod

SCALE = (1, 2, 3, 5, 8, 13)
#: The weight at which each value of the scale starts: 0 is one point, 7 or more thirteen.
STARTS = (0, 1, 2, 3, 5, 7)

#: (at or above, weight) for each signal of the estimate.
CRITERIA_WEIGHTS = ((2, 1), (4, 2), (7, 3))
NAMED_WEIGHTS = ((3, 1), (6, 2))
REACHED_WEIGHTS = ((5, 1), (20, 2))
#: The kinds whose work is larger than their words: everything else weighs nothing for its kind.
KIND_WEIGHTS = {"feature": 1, "debug-unclear": 1, "review-critical": 1, "plan": 1, "frontier": 2}
#: The areas of risk counted, one weight each: a third says no more than two did.
MAX_AREAS = 2
#: The length at which a request weighs one, and never more: the weakest signal.
LONG_CHARS = 2000

#: (at or above, weight) for each measure of the realized size.
FILES_WEIGHTS = ((3, 1), (8, 2), (20, 3))
LINES_WEIGHTS = ((50, 1), (200, 2), (600, 3), (1500, 4))
TURNS_WEIGHTS = ((30, 1), (80, 2), (160, 3))
MINUTES_WEIGHTS = ((10, 1), (30, 2), (90, 3))
ATTEMPTS_WEIGHTS = ((2, 1), (4, 2))

_EXT = (r"py|pyi|ts|tsx|js|jsx|mjs|cjs|json|md|toml|ya?ml|sql|go|rs|java|kts?|cs|rb|php|swift|lua|c|cc|cpp|h|hpp|"
        r"sh|css|scss|html|vue|svelte|astro|txt|cfg|ini|lock|xml|proto|graphql|tf")
#: A path a request names: a glob, a file with a folder, a folder ending in `/`, or a file name
#: with an extension. Never part of a URL or a longer word.
_PATH = re.compile(
    r"(?<![\w@/.:-])(?:"
    r"(?:[\w.-]+/)+[\w.-]*\*[\w.*/-]*"
    r"|(?:[\w.-]+/)+[\w.-]*\.[A-Za-z0-9]{1,5}\b"
    r"|(?:[\w.-]+/)+(?![\w*.])"
    rf"|[\w-]+\.(?:{_EXT})\b(?!\.\w)"
    r")")


@dataclass(frozen=True)
class Score:
    points: int
    reasons: tuple[str, ...] = ()

    def line(self) -> str:
        return f"{self.points}: {', '.join(self.reasons) or 'nothing says it is bigger'}"

    def data(self) -> dict[str, object]:
        return {"points": self.points, "reasons": list(self.reasons)}


def on_scale(weight: int) -> int:
    """The value of the scale a weight lands on."""
    return SCALE[max(i for i, start in enumerate(STARTS) if weight >= start)]


def _weight(value: float, steps: Sequence[tuple[float, int]]) -> int:
    return max((w for at, w in steps if value >= at), default=0)


def _words(found: Sequence[str]) -> str:
    return found[0] if len(found) == 1 else f"{', '.join(found[:-1])} and {found[-1]}"


#: How much bigger than their estimate a kind's tasks must have turned out here, at the median over at
#: least `UNDER_SAMPLES` of them, before its next estimate is raised one step of the scale.
UNDER_RATIO, UNDER_SAMPLES = 1.5, 5


def corrected(score: Score, kind: str, ratio: float | None, samples: int) -> Score:
    """An estimate raised one step of the scale when the kind's tasks here turned out
    `UNDER_RATIO` or more times their estimate (realized over estimated, at the median of
    `samples`). Only raised, one step, and said; never lowered, since a size too small is
    the one that splits too late."""
    if ratio is None or samples < UNDER_SAMPLES or ratio < UNDER_RATIO or score.points >= SCALE[-1]:
        return score
    up = SCALE[SCALE.index(score.points) + 1]
    return Score(up, (*score.reasons, f"{kind} tasks here turned out {ratio:.1f}× their estimate "
                                      f"(median of {samples}): one step up"))


#: Words that ask for a change across the whole product, not in one place: the work reaches every
#: occurrence, which no list of symbols names up front (two 8- and 13-point builds ran out of turns on
#: their first try with "remove … everywhere" in their request). Spanish because people type it.
_SWEEPING = re.compile(
    r"\b(everywhere|across (the )?(whole |entire )?(app|codebase|repo(sitory)?|project|product|site)|"
    r"(all|every) (the )?(screens?|pages?|views?|files?|components?|places|occurrences|usages|uses)|"
    r"wherever it (appears|is used)|"
    r"en todas? (las? )?(partes|pantallas|p[aá]ginas|vistas|archivos|componentes)|"
    r"en todo el (proyecto|c[oó]digo|repo(sitorio)?|sitio)|en toda la (app|aplicaci[oó]n))\b", re.I)
SWEEPING_WEIGHT = 2


def sweeping(text: str) -> str | None:
    """The words with which a request asks for a change everywhere, if it does."""
    found = _SWEEPING.search(text)
    return found.group(0) if found else None


def estimate(*, criteria: int, kind: str, tier: str, areas: Sequence[tuple[str, str]], named: int,
             reached: int | None, chars: int, sweep: str | None = None) -> Score:
    """A task's size before the work, from the plan's signals. `reached` is None when no
    readable index said it: then it says nothing, rather than zero."""
    weight, reasons = 0, []

    def add(value: int, reason: str) -> None:
        nonlocal weight
        if value:
            weight += value
            reasons.append(reason)

    add(_weight(criteria, CRITERIA_WEIGHTS), f"{criteria} criteria")
    words = list(dict.fromkeys(word for _, word in areas))
    add(min(len({area for area, _ in areas}), MAX_AREAS), f"touches {_words(words)}" if words else "")
    add(1 if tier == "high" else 0, "risk high")
    add(KIND_WEIGHTS.get(kind, 0), f"kind {kind}")
    add(_weight(named, NAMED_WEIGHTS), f"names {named} files")
    add(_weight(reached or 0, REACHED_WEIGHTS), f"reaches {reached} symbols")
    add(1 if chars >= LONG_CHARS else 0, f"long request ({chars} chars)")
    add(SWEEPING_WEIGHT if sweep else 0, f"asks for it everywhere ({sweep!r})")
    return Score(on_scale(weight), tuple(reasons))


def named(text: str, surfaces: Sequence[str] = ()) -> list[str]:
    """The files and folders a request names, and the surfaces it was given, once each."""
    return sorted({*surfaces, *(m.group(0) for m in _PATH.finditer(text))})


def of_request(text: str, kind: str, risk: risk_mod.Risk, *, surfaces: Sequence[str] = (),
               reached: int | None = None) -> Score:
    """The estimate a plan makes of its request."""
    return estimate(criteria=len(criteria_mod.items(text)), kind=kind, tier=risk.tier, areas=risk_mod.areas(risk),
                    named=len(named(text, surfaces)), reached=reached, chars=len(text), sweep=sweeping(text))


def realized(*, files: int, lines: int | None, turns: int, minutes: float | None, attempts: int) -> Score:
    """A task's size after the work, from what was measured. What could not be measured
    (`None`: no branch to count lines on, no wall time kept) is left out, never guessed."""
    weight, reasons = 0, []
    for value, steps, reason in (
            (files, FILES_WEIGHTS, f"{files} files"),
            (lines, LINES_WEIGHTS, f"{lines} lines"),
            (turns, TURNS_WEIGHTS, f"{turns} turns"),
            (minutes, MINUTES_WEIGHTS, f"{minutes or 0:.0f} minutes"),
            (attempts, ATTEMPTS_WEIGHTS, f"{attempts} attempts")):
        found = _weight(value, steps) if value is not None else 0
        if found:
            weight += found
            reasons.append(reason)
    return Score(on_scale(weight), tuple(reasons))
