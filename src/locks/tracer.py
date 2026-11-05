"""The TRACER method in a repository that keeps it (`docs/tracer/`): read, never written.

TRACER (github.com/Rixmerz/tracer) defines a product from its audience's side before it is built:
audience profiles, critical flows whose acceptance lines come in two halves (WORKS, the technical
result; CORRECT, what the person sees and can do), component floors and a catalogue of UI/UX
anti-patterns. Its phases that need the person (the contract, the empathy round, the scope filter)
stay in the session; what locks does is carry the written method into the work it runs:

- a writing worker gets the audience, the floors, the anti-patterns, the identity and the flows its
  request names, as data, in its brief;
- the independent verifier gets the CORRECT lines of those flows and the anti-pattern catalogue as
  what the change must meet: a line that does not hold, or an anti-pattern shown, is a finding.

Nothing here is a model call, and nothing is written in the repository.
"""
from __future__ import annotations

import re
from itertools import groupby
from pathlib import Path

from locks.text import clip, fold, outline, words

FOLDER = Path("docs") / "tracer"
#: The method's files a worker reads before any code, in this order, and what each holds.
FILES = (("audiences.md", "Audience profiles"), ("identity.md", "Identity guideline"),
         ("floors.md", "Component floors"), ("anti-patterns.md", "Anti-pattern catalogue"))
#: The most of the method one brief carries; what is left out is said by count, with where it is.
BRIEF_CHARS = 8000
#: The most of a brief the index of what did not fit takes (`text.outline`); further sections are counted.
OUTLINE_CHARS = 2000


def present(repo_dir: Path) -> bool:
    return (repo_dir / FOLDER).is_dir()


def _read(path: Path) -> str:
    try:
        # Right-stripped only: a section index names lines (`text.outline`), which leading blank lines would shift.
        return path.read_text(encoding="utf-8", errors="replace").rstrip()
    except OSError:
        return ""


#: `Flow: …` and `Capability: …` as the critical-flow template writes them (markdown emphasis removed first).
_LABEL = re.compile(r"^\s*(flow|capability)\s*:\s*(.+)$", re.IGNORECASE)
_HEADING = re.compile(r"^#+\s+(.+)$")
_FLOW_PREFIX = re.compile(r"^(?:critical\s+)?flow\s*:\s*", re.IGNORECASE)
#: Words every flow file carries, so naming one names no flow in particular.
_GENERIC = {"critical", "flow", "flows"}
#: An id or a link after a capability's name in the capability map: `CAP-3: …`, `… (REQ-1)`, `… — requirements`.
_CAPABILITY_ID = re.compile(r"^[A-Za-z]+-\d+\s*[:.\u2014\u2013-]?\s*")
_CAPABILITY_TAIL = re.compile(r"\s[\u2014\u2013-]\s|\s\(|:|\[")
_LIST_ENTRY = re.compile(r"^(?:#+|[-*+]|\d+[.)])\s+")


def _titles(body: str) -> list[str]:
    """What a flow file calls itself: its `Flow:` and `Capability:` lines and its first heading."""
    found, heading = [], False
    for raw in body.splitlines():
        line = raw.replace("*", "").replace("`", "")
        match, title = _LABEL.match(line), _HEADING.match(line)
        if title and not heading:
            heading = True
            found.append(_FLOW_PREFIX.sub("", title.group(1)))
        elif match and not title:
            found.append(match.group(2))
    return found


def capability_names(repo_dir: Path) -> list[str]:
    """The names in the capability map (`docs/tracer/capabilities.md`): its headings, its unindented list
    entries and the first column of a table, each without the id or the requirement links after it."""
    names = []
    for raw in _read(repo_dir / FOLDER / "capabilities.md").splitlines():
        line = raw.replace("*", "").replace("`", "")
        entry = _LIST_ENTRY.match(line)
        if entry:
            name = line[entry.end():]
        elif line.startswith("|") and not _SEPARATOR.match(line):
            name = _cells(line)[0]
        else:
            continue
        name = _CAPABILITY_TAIL.split(_CAPABILITY_ID.sub("", name.strip()), maxsplit=1)[0].strip()
        if name and fold(name) not in ("capability", "capabilities"):
            names.append(name)
    return names


def _covered(named: list[str], asked: set[str], at_least: int) -> bool:
    """Every word of a name is in the request, or begins one of its words (`print` for "printed")."""
    core = [w for w in named if w not in _GENERIC]
    return len(core) >= at_least and all(
        any(a == w or (len(w) >= 4 and a.startswith(w)) for a in asked) for w in core)


def _reached(stem: list[str], titles: list[list[str]], asked: set[str], capabilities: list[list[str]]) -> bool:
    """A flow is named by its file name, by a title of its own with two words or more, or by a capability of
    the map the request names whose words all lie in one of those: the flow file belongs to that capability."""
    if _covered(stem, asked, 1) or any(_covered(t, asked, 2) for t in titles):
        return True
    return any(set(c) <= set(w) for c in capabilities for w in (stem, *titles))


def flows_for(repo_dir: Path, text: str) -> list[Path]:
    """The critical flows a request reaches: a file under `docs/tracer/flows/` whose name's words all
    appear in the request, or begin one of its words (`flows/print-receipt.md` for "...the printed
    receipts..."); or one whose own title (its `Flow:` or `Capability:` line, its heading) holds two
    words or more that all appear; or one that belongs to a capability of `capabilities.md` the request
    names. Strict on purpose: a flow briefed by a loose match puts another capability's CORRECT lines
    on the work."""
    folder = repo_dir / FOLDER / "flows"
    if not folder.is_dir():
        return []
    asked = set(words(text))
    capabilities = [w for w in (words(n) for n in capability_names(repo_dir)) if _covered(w, asked, 2)]
    out = []
    for path in sorted(folder.glob("*.md")):
        stem = words(path.stem.replace("-", " ").replace("_", " "))
        if _reached(stem, [words(t) for t in _titles(_read(path))], asked, capabilities):
            out.append(path)
    return out


def _sections(repo_dir: Path, text: str) -> list[tuple[str, str, str]]:
    """Each method file a brief carries: its title, its path from the repository, its text."""
    found = [(title, (FOLDER / name).as_posix(), _read(repo_dir / FOLDER / name)) for name, title in FILES]
    found += [("Critical flow", path.relative_to(repo_dir).as_posix(), _read(path))
              for path in flows_for(repo_dir, text)]
    return [(title, rel, body) for title, rel, body in found if body]


def brief_lines(repo_dir: Path, text: str) -> list[str]:
    """What a writing worker reads before any code: the method's files, as data."""
    if not present(repo_dir):
        return []
    sections = _sections(repo_dir, text)
    lines = [f"This repository follows the TRACER method (`{FOLDER.as_posix()}/`): done means correct for the "
             "people who use it, checked through the interface they use, never only that tests pass. Read these "
             "before any code. Never fill a detail they leave open with the most common solution: say it in your "
             "result instead. Each CORRECT line of a flow below is part of what your work must meet."]
    room, left, index = BRIEF_CHARS, 0, []
    for title, rel, body in sections:
        if room <= 200:
            left += 1
            index += outline(rel, body) or [f"{title} ({rel})"]
            continue
        shown = clip(body, room)
        room -= len(shown)
        lines.append(f"{title} ({rel}):\n{shown}")
        if len(shown) < len(body):
            # What did not fit is named by section with its lines, so the worker reads only the part it needs.
            index += outline(rel, body, after=shown.count("\n"))
    if index:
        fits, room = [], OUTLINE_CHARS
        for entry in index:
            if len(entry) + 1 > room:
                break
            fits.append(entry)
            room -= len(entry) + 1
        more = len(index) - len(fits)
        lines.append("Not shown above, by section (read the lines you need):\n" + "\n".join(f"- {e}" for e in fits)
                     + (f"\n{more} more section(s) in `{FOLDER.as_posix()}/`." if more else ""))
    if not flows_for(repo_dir, text) and (repo_dir / FOLDER / "flows").is_dir():
        lines.append(f"No critical flow under `{FOLDER.as_posix()}/flows/` is named by this request: read the "
                     "ones your change reaches.")
    return lines


#: A line that opens a CORRECT criterion as the template writes it: `- CORRECT: …` (bold or not).
_CORRECT_HEAD = re.compile(r"^(\s*)([-*+]\s+)?\**CORRECT\**\s*:")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SEPARATOR = re.compile(r"^[\s|:-]+$")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_correct(block: list[str]) -> list[str]:
    """A table whose header row has a CORRECT column (the template's failure paths): that column's
    cell of each row, led by the row's first cell; the header row itself is no criterion."""
    header = _cells(block[0])
    column = next((i for i, cell in enumerate(header) if cell.upper().startswith("CORRECT")), None)
    if column is None:
        return []
    out = []
    for row in block[1:]:
        cells = _cells(row)
        if _SEPARATOR.match(row) or column >= len(cells) or not cells[column]:
            continue
        out.append(f"{cells[0]}: {cells[column]}" if column else cells[column])
    return out


def _continues(line: str, indent: int, bullet: bool) -> bool:
    """A line that goes on with the CORRECT line above it: deeper in, or (for a bare `CORRECT:` that is
    no list item) the list items right under it."""
    if not line.strip() or line.lstrip().startswith("|") or _CORRECT_HEAD.match(line):
        return False
    here = len(line) - len(line.lstrip())
    return here > indent or (not bullet and here == indent and bool(_ITEM.match(line)))


def _go_on(entries: list[str], line: str) -> None:
    """A line under a CORRECT head: an item is a line of its own, any other line the one above wrapped."""
    item = _ITEM.match(line)
    if item:
        entries.append(line[item.end():].strip())
    elif entries[-1]:
        entries[-1] += " " + line.strip()
    else:
        entries.append(line.strip())


def _bullet_correct(lines: list[str]) -> list[str]:
    """Each `CORRECT:` criterion with what goes on under it."""
    out, i = [], 0
    while i < len(lines):
        head = _CORRECT_HEAD.match(lines[i])
        i += 1
        if not head:
            continue
        entries = [lines[i - 1][head.end():].strip(" *")]
        while i < len(lines) and _continues(lines[i], len(head.group(1)), bool(head.group(2))):
            _go_on(entries, lines[i])
            i += 1
        out += [e for e in entries if e]
    return out


def correct_lines(body: str) -> list[str]:
    """The CORRECT criteria of a critical flow, read as TRACER's template defines them: `CORRECT:` items of
    the success path (with their continuation lines) and the CORRECT column of the failure-path table."""
    lines = body.splitlines()
    out = _bullet_correct(lines)
    for is_table, block in groupby(lines, key=lambda line: line.lstrip().startswith("|")):
        if is_table:
            out += _table_correct(list(block))
    return out


#: How the measuring neighbours check what TRACER asks of an interface, when the check has them.
MEASURED_BY = {
    "layout-inspector": "Measure the floors and anti-patterns on the rendered page instead of judging them by eye; "
                        "the tool for each is named beside it below. A floor or anti-pattern a tool shows broken is "
                        "a `deterministic` finding with the measurement as its proof; one that no tool measures is "
                        "judged from the step or the rendered text, never reported as measured.",
}

#: What each measuring neighbour's tools can say of a floor or anti-pattern line: the words that
#: point at it (folded, so no accents) and what is measured. A line may match several. Only what the
#: installed tool measures is listed: layout-inspector has no focus or keyboard check; contrast and
#: design-system tokens come from `check_design_system` (0.6).
_Rule = tuple[re.Pattern[str], str]
MEASURE_RULES: dict[str, tuple[_Rule, ...]] = {
    "layout-inspector": tuple((re.compile(pattern), how) for pattern, how in (
        (r"target|tap |touch|hit area|click area|\b(24|44) ?px",
         "`accessibility_spatial` (WCAG target sizes)"),
        (r"cover|obscur|unreachable|cannot be (clicked|tapped|reached)|blocked by|behind (a|an|the) ",
         "`accessibility_spatial` (covered controls), `element_context` for what covers it"),
        (r"overflow|scroll|clip|cut off|truncat|ellipsis|off-?screen|spill",
         "`detect_issues` (horizontal scroll, clipping, truncated text), `element_context` for the cause"),
        (r"overlap|z-?index|stack|occlu|on top of|collid",
         "`detect_issues` (overlap, occluded content, z-index conflicts)"),
        (r"breakpoint|responsive|mobile|phone|tablet|desktop|viewport|narrow|wide screen|small screen",
         "`compare_viewports` for every breakpoint the audience uses"),
        (r"contrast|colou?r|design system|design token|\btokens?\b|off-system",
         "`check_design_system` (text contrast on the render, values off the design system's tokens)"),
        (r"keyboard|tab order|focus|screen reader|aria|alt text",
         "no tool measures it (layout-inspector has no focus or keyboard check): judge it from the "
         "rendered text"),
    )),
}
_BULLET = re.compile(r"^[\s|>*#-]+|[\s|]+$")
_LINE_CHARS = 140


def _requirement_lines(repo_dir: Path) -> list[tuple[str, str]]:
    """The floors and anti-patterns of the method, a line each: `(file, line)`."""
    out = []
    for name in ("floors.md", "anti-patterns.md"):
        for raw in _read(repo_dir / FOLDER / name).splitlines():
            line = _BULLET.sub("", raw)
            if any(c.isalpha() for c in line) and len(line) > 3 and not set(line) <= set("-|: "):
                out.append((name, line))
    return out


def _measured_lines(repo_dir: Path, tool: str) -> list[str]:
    """Each floor and anti-pattern of the repository's method beside the tool of `tool` that measures
    it, one line each; the ones no rule reaches are counted, never dropped silently. Bounded as a brief
    is: what does not fit is said by count."""
    rules = MEASURE_RULES.get(tool, ())
    lines, unmatched, left, room = [], 0, 0, BRIEF_CHARS // 2
    for name, line in _requirement_lines(repo_dir):
        seen = fold(line)
        how = [h for pattern, h in rules if pattern.search(seen)]
        if not how:
            unmatched += 1
            continue
        entry = f"- {clip(line, _LINE_CHARS, one_line=True)} ({name.removesuffix('.md')}) -> {'; '.join(how)}"
        if room < len(entry):
            left += 1
            continue
        room -= len(entry)
        lines.append(entry)
    if left:
        lines.append(f"{left} of the floor and anti-pattern lines did not fit here: read "
                     f"`{(FOLDER / 'floors.md').as_posix()}` and `{(FOLDER / 'anti-patterns.md').as_posix()}`.")
    if unmatched:
        lines.append(f"{unmatched} of the floor and anti-pattern lines name nothing {tool} measures: judge "
                     "them from the step or the rendered text.")
    return lines


def measured_by(name: str) -> str | None:
    """How a measuring neighbour checks the method: locks' framing, with what each of its tools
    measures in its own installed manifest's words when it has them (`locks.manifests`, the person's
    plugin cache, never this repository), else the built-in text alone."""
    from locks import manifests

    if name not in MEASURED_BY:
        return None
    own = manifests.read(name)
    if own is None or not own.measures:
        return MEASURED_BY[name]
    tools = "\n".join(f"- `{tool}`: {what}" for tool, what in sorted(own.measures.items()))
    return f"{MEASURED_BY[name]}\nWhat each tool measures, as {own.plugin} {own.version} says:\n{tools}"


def criteria(repo_dir: Path, text: str) -> tuple[list[tuple[str, str]], int]:
    """The CORRECT lines of the flows a request names as the independent check's criteria, `C1..Cn`
    in order (`flow: line`), as many as fit in half of `BRIEF_CHARS`, and how many more there are: a
    line that does not fit is said by count and asked of no one, never dropped unsaid."""
    if not present(repo_dir):
        return [], 0
    lines = [f"{path.stem}: {line}" for path in flows_for(repo_dir, text) for line in correct_lines(_read(path))]
    listed: list[tuple[str, str]] = []
    room = BRIEF_CHARS // 2
    for line in lines:
        if len(line) + 6 > room:
            break
        listed.append((f"C{len(listed) + 1}", line))
        room -= len(line) + 6
    return listed, len(lines) - len(listed)


def verifier_lines(repo_dir: Path, text: str, measures: tuple[str, ...] = ()) -> list[str]:
    """What the independent check must hold the change to: the CORRECT lines of the flows the
    request names and the anti-pattern catalogue, judged as the audience would; with a measuring
    neighbour it has (`measures`), how to measure them."""
    if not present(repo_dir):
        return []
    correct, more = criteria(repo_dir, text)
    catalogue = _read(repo_dir / FOLDER / "anti-patterns.md")
    if not correct and not catalogue and not (measures and _requirement_lines(repo_dir)):
        return []
    out = [f"This repository follows the TRACER method: judge the change as the audience in "
           f"`{FOLDER.as_posix()}/audiences.md` would. A CORRECT line below that does not hold, or an anti-pattern "
           "of the catalogue the change shows, is a finding, shown with the step or the rendered text that proves it."]
    if correct:
        out.append("CORRECT lines of the flows this request names. Each is a criterion: answer every one in "
                   "`criteria` by its id, whether the change meets it, and what shows it (the step you took, the "
                   "probe, the rendered text). One you leave out leaves the check undecided:\n"
                   + "\n".join(f"{cid}: {line}" for cid, line in correct)
                   + (f"\n({more} more CORRECT line(s) did not fit here; judge them with the flows' files)"
                      if more else ""))
    if catalogue:
        out.append("Anti-pattern catalogue:\n" + clip(catalogue, BRIEF_CHARS // 2))
    for name in measures:
        if (how := measured_by(name)) is not None:
            out.append(how)
            by_line = _measured_lines(repo_dir, name)
            if by_line:
                out.append("Floors and anti-patterns, with the tool that measures each:\n" + "\n".join(by_line))
    return out
