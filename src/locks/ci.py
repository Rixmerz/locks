"""What a repository's own checks run, read from its files: the scripts a command
names (package.json `scripts`, deno.json `tasks`), whether its tests drive a
headless browser, and the jobs its CI runs (`.github/workflows/*.yml`) with the
services and environment each sets.

Read only, and no YAML library: a small line reader takes the shapes workflows
are written in (mappings, lists, block scalars, comments); a file it cannot
read is skipped and said. What is found here is a note for the plan, never a
verdict and never a blocker.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locks import repo
from locks.text import clip

#: How many scripts deep a command is followed.
DEPTH = 4
#: How many jobs one note names.
JOBS_NAMED = 5
#: What drives a headless browser, in a command or a dependency.
_BROWSER_TOOLS = re.compile(r"(@playwright/test|\bplaywright-core\b|\bplaywright\b|\bpuppeteer\b|\bcypress\b)",
                            re.IGNORECASE)
_BROWSER_PACKAGES = ("@playwright/test", "playwright-core", "playwright")
_LOCKED_BROWSER = re.compile(r"\bnpm:(@playwright/test|playwright-core|playwright)@")
#: A command that runs a named script: `npm test`, `npm run e2e`, `pnpm e2e`, `deno task check`.
_NPM_SCRIPT = re.compile(r"\b(?:npm\s+(?:run(?:-script)?\s+([\w:.-]+)|(test|t|start)\b)|"
                         r"(?:pnpm|yarn)\s+(?:run\s+)?([\w:.-]+)|bun\s+run\s+([\w:.-]+))")
_DENO_TASK = re.compile(r"\bdeno\s+task\s+([\w:.-]+)")
#: Jobs that deliver rather than check: no local check stands in for them.
_DELIVERY = re.compile(r"deploy|release|publish|pages", re.IGNORECASE)
#: A shell line continued on the next one: one command, read as one.
_CONTINUED = re.compile(r"\s*\\\n\s*")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: A CI line that readies the job rather than checks the work: an install, a browser
#: download, a tool's version, a shell's own bookkeeping, an assignment alone. No local check stands for it.
_SETUP = re.compile(
    r"^(?:sudo\s+)?(?:(?:npm|pnpm|yarn|bun)\s+(?:ci|install|i|add)\b|yarn$|deno\s+(?:install|cache)\b|"
    r"(?:python3?\s+-m\s+)?pip3?\s+install\b|uv\s+(?:sync|venv|pip\s+install|tool\s+install)\b|poetry\s+install\b|"
    r"npx\s+(?:(?:--yes|-y)\s+)?playwright(?:-core)?(?:@\S+)?\s+install\b|(?:apt|apt-get|brew)\s|"
    r"(?:cd|echo|printf|export|mkdir|cp|mv|rm|ln|chmod|cat|ls|sleep|source|git\s+config|curl|wget)\b|"
    r"(?:until|while|for|do|done|if|then|else|fi)\b|[\w.-]+\s+(?:--version|-V|version)$|"
    r"(?:node|npm|npx|pnpm|yarn|bun|deno|tsc|go|java|ruby)\s+-v$|"
    r"(?:[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s*)+$)")


def _parts(line: str) -> list[str]:
    """A shell line's commands, split where `&&`, `||` or `;` ends one outside quotes."""
    parts, start, quote, i = [], 0, "", 0
    while i < len(line):
        c = line[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == ";" or line.startswith(("&&", "||"), i):
            parts.append(line[start:i])
            i += 1 if c == ";" else 2
            start = i
            continue
        i += 1
    return [p.strip() for p in [*parts, line[start:]] if p.strip()]


class Unreadable(ValueError):
    """A workflow file whose shape the line reader does not take."""


# --- scripts ---------------------------------------------------------------------------


def _json(path: Path, *, comments: bool = False) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if comments:
        # JSONC: comments out (never inside a string), then trailing commas.
        text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.DOTALL)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except ValueError:
        return None


def _places(where: Path) -> list[Path]:
    """The task's directory and its checkout's top, where a project keeps its manifests."""
    top = repo.toplevel(where)
    return list(dict.fromkeys([where, *([top] if top else [])]))


def _package(where: Path) -> dict[str, Any]:
    for place in _places(where):
        found = _json(place / "package.json")
        if isinstance(found, dict):
            return found
    return {}


def _deno(where: Path) -> tuple[dict[str, Any], str]:
    for place in _places(where):
        for name in ("deno.json", "deno.jsonc"):
            found = _json(place / name, comments=name.endswith("c"))
            if isinstance(found, dict):
                return found, name
    return {}, ""


def _package_scripts(where: Path) -> dict[str, str]:
    raw = _package(where).get("scripts")
    return {k: v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}


def _deno_tasks(where: Path) -> dict[str, str]:
    """deno.json's tasks, each as the commands it runs: a task's `dependencies` run before it."""
    raw = _deno(where)[0].get("tasks")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, task in raw.items():
        if isinstance(task, str):
            out[name] = task
        elif isinstance(task, dict) and isinstance(task.get("command"), str):
            first = [f"deno task {d}" for d in task.get("dependencies") or () if isinstance(d, str)]
            out[name] = " && ".join([*first, task["command"]])
    return out


def scripts(where: Path) -> dict[str, str]:
    """The project's named scripts: package.json's and deno.json's tasks."""
    return {**_package_scripts(where), **_deno_tasks(where)}


def runs(command: str, where: Path) -> list[str]:
    """`command`, then the body of every script it names, and theirs, `DEPTH` deep:
    what running it runs."""
    npm, deno = _package_scripts(where), _deno_tasks(where)
    out, seen, todo = [command], set(), [(command, 0)]
    while todo:
        text, depth = todo.pop(0)
        if depth >= DEPTH:
            continue
        named = [("npm", next(g for g in m.groups() if g)) for m in _NPM_SCRIPT.finditer(text)]
        named += [("deno", m.group(1)) for m in _DENO_TASK.finditer(text)]
        for runner, name in named:
            body = (deno if runner == "deno" else npm).get(name)
            if body is None or (runner, name) in seen:
                continue
            seen.add((runner, name))
            out.append(body)
            todo.append((body, depth + 1))
    return out


def _browser_dependency(where: Path) -> str | None:
    """A headless browser among the project's dependencies, and where it is declared."""
    package = _package(where)
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        deps = package.get(section)
        found = next((p for p in _BROWSER_PACKAGES if isinstance(deps, dict) and p in deps), None)
        if found:
            return f"{found} in package.json"
    deno, name = _deno(where)
    imports = deno.get("imports") if isinstance(deno.get("imports"), dict) else {}
    for spec in imports.values():
        if isinstance(spec, str) and (m := _LOCKED_BROWSER.search(spec + "@")):
            return f"{m.group(1)} in {name}"
    for place in _places(where):
        try:
            locked = (place / "deno.lock").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if m := _LOCKED_BROWSER.search(locked):
            return f"{m.group(1)} in deno.lock"
    return None


def browser(where: Path, verify: str | None) -> str | None:
    """Why the task's check, or the project's own tests, drive a headless browser; None when neither does."""
    if verify and (m := _BROWSER_TOOLS.search(" \n".join(runs(verify, where)))):
        return f"its check runs {m.group(1).lower()} (`{verify}`)"
    if found := _browser_dependency(where):
        return f"the project's own tests drive a headless browser ({found})"
    for name, body in scripts(where).items():
        if "e2e" in name.lower() and (m := _BROWSER_TOOLS.search(" \n".join(runs(body, where)))):
            return f"the project's own tests drive a headless browser (its {name} script runs {m.group(1).lower()})"
    return None


# --- the line reader ----------------------------------------------------------------------

_KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s"'#\-][^:#]*?|-[^\s:#][^:#]*?)\s*:(?:\s+(?P<rest>.*))?$""")
_BLOCK = re.compile(r"^[|>][-+]?\d*$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _item(stripped: str) -> bool:
    return stripped == "-" or stripped.startswith("- ")


def _next(lines: list[str], i: int) -> int:
    """The next line that holds something: not blank, not a comment, not a document marker."""
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and stripped not in ("---", "..."):
            return i
        i += 1
    return i


def _plain(rest: str) -> str | None:
    """A scalar written on its key's line: quoted, or plain with its comment cut."""
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        while quote == "'" and end >= 0 and rest[end + 1:end + 2] == "'":
            end = rest.find("'", end + 2)
        if end < 0:
            raise Unreadable(f"an unclosed quote: {rest[:40]}")
        body = rest[1:end]
        return body.replace("''", "'") if quote == "'" else body.replace('\\"', '"')
    value = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
    return value if value not in ("", "~", "null") else None


def _text_block(lines: list[str], i: int, indent: int) -> tuple[str, int]:
    """A `|` or `>` scalar: the lines indented past its key, their common indent taken off."""
    held = []
    while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > indent):
        held.append(lines[i])
        i += 1
    while held and not held[-1].strip():
        held.pop()
    cut = min((_indent(line) for line in held if line.strip()), default=0)
    return "\n".join(line[cut:] for line in held), i


def _value(lines: list[str], i: int, indent: int, rest: str) -> tuple[Any, int]:
    """What a key or a list item at `indent` on line `i` holds, `rest` being what follows it there."""
    if _BLOCK.match(rest.split(" #")[0].strip()) and rest:
        return _text_block(lines, i + 1, indent)
    if rest and not rest.startswith("#"):
        # An empty flow collection is the one flow form that says something: no keys, no items
        # (`db: {}` named a CI service `{}`). Others stay text, as the reader's docstring says.
        bare = rest.split(" #")[0].strip()
        if bare in ("{}", "[]"):
            return ({} if bare == "{}" else []), i + 1
        return _plain(rest), i + 1
    j = _next(lines, i + 1)
    if j < len(lines) and _indent(lines[j]) > indent:
        return _node(lines, j)
    if j < len(lines) and _indent(lines[j]) == indent and _item(lines[j].strip()):
        return _sequence(lines, j, indent)
    return None, j


def _node(lines: list[str], i: int) -> tuple[Any, int]:
    i = _next(lines, i)
    if i >= len(lines):
        return None, i
    if _item(lines[i].strip()):
        return _sequence(lines, i, _indent(lines[i]))
    return _mapping(lines, i, _indent(lines[i]))


def _mapping(lines: list[str], i: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while True:
        i = _next(lines, i)
        if i >= len(lines) or _indent(lines[i]) != indent or _item(lines[i].strip()):
            return out, i
        found = _KEY.match(lines[i].strip())
        if not found:
            raise Unreadable(f"line {i + 1} is no key: {lines[i].strip()[:40]}")
        key = found.group("key").strip("\"'")
        out[key], i = _value(lines, i, indent, (found.group("rest") or "").strip())


def _sequence(lines: list[str], i: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while True:
        i = _next(lines, i)
        if i >= len(lines) or _indent(lines[i]) != indent or not _item(lines[i].strip()):
            return out, i
        content = lines[i].strip()[1:]
        at = indent + 1 + _indent(content)
        content = content.strip()
        if content and _KEY.match(content) and content[:1] not in ("'", '"', "[", "{"):
            # `- run: x`: a mapping whose first key sits on the item's line.
            lines[i] = " " * at + content
            value, i = _mapping(lines, i, at)
        else:
            value, i = _value(lines, i, indent, content)
        out.append(value)


def read(text: str) -> Any:
    """A workflow's YAML, in the shapes workflows are written in: mappings, lists, plain
    and quoted scalars, `|` and `>` blocks. Flow collections (`[a, b]`) stay text.
    Raises `Unreadable` for a shape it does not take, never guesses past one."""
    lines = text.replace("\t", "  ").splitlines()
    value, i = _node(lines, 0)
    if _next(lines, i) < len(lines):
        raise Unreadable(f"line {_next(lines, i) + 1} is out of place: {lines[_next(lines, i)].strip()[:40]}")
    if value is None or isinstance(value, str):
        raise Unreadable("no mapping")
    return value


# --- CI ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    name: str
    file: str
    #: Each line of each `run:`, in order.
    commands: tuple[str, ...]
    #: Each service by what it runs: `postgres` for `image: postgres:16`.
    services: tuple[str, ...]
    #: The environment it sets: the workflow's, the job's and its steps'. Names only.
    env: tuple[str, ...]


def _keys(raw: Any) -> list[str]:
    return [str(k) for k in raw] if isinstance(raw, dict) else []


def _service(name: str, spec: Any) -> str:
    image = spec.get("image") if isinstance(spec, dict) else spec
    if isinstance(image, str) and image.strip():
        return image.strip().split(":")[0].rsplit("/", 1)[-1]
    return name


#: The triggers that make a workflow a check of a change: it runs on a push or a pull request. One
#: that runs on a schedule, by hand or after another workflow checks no change of a task's.
CHANGE_TRIGGERS = frozenset({"push", "pull_request", "pull_request_target", "merge_group"})


def checks_changes(doc: Mapping[str, Any]) -> bool:
    """Whether a workflow runs on a change (`CHANGE_TRIGGERS`); one with no readable `on` is taken as one."""
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        # `on: push`, or a flow list the line reader keeps as text: `on: [push, pull_request]`.
        return bool(set(re.findall(r"[a-z_]+", on)) & CHANGE_TRIGGERS)
    if isinstance(on, (list, dict)):
        return any(str(t) in CHANGE_TRIGGERS for t in on)
    return True


def jobs(where: Path) -> tuple[list[Job], list[str]]:
    """The jobs of the repository's CI, and the workflow files that could not be read."""
    top = repo.toplevel(where) or where
    folder = top / ".github" / "workflows"
    try:
        files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]) if folder.is_dir() else []
    except OSError:
        return [], []
    found: list[Job] = []
    unreadable: list[str] = []
    for path in files:
        rel = path.relative_to(top).as_posix()
        try:
            doc = read(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, Unreadable, RecursionError):
            unreadable.append(rel)
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            unreadable.append(rel)
            continue
        if not checks_changes(doc):
            # A workflow that runs on a schedule, by hand or after another (a weekly pin, a release)
            # checks no change: its jobs are not what a task's check leaves out.
            continue
        shared = _keys(doc.get("env"))
        for name, job in doc["jobs"].items():
            if not isinstance(job, dict):
                continue
            steps = [s for s in job.get("steps") or () if isinstance(s, dict)] if isinstance(
                job.get("steps"), list) else []
            commands = tuple(line.strip() for s in steps if isinstance(s.get("run"), str)
                             for line in _CONTINUED.sub(" ", s["run"]).splitlines()
                             if line.strip() and not line.strip().startswith("#"))
            services = job.get("services") if isinstance(job.get("services"), dict) else {}
            env = [*shared, *_keys(job.get("env")), *(k for s in steps for k in _keys(s.get("env")))]
            found.append(Job(str(name), rel, commands, tuple(_service(str(n), s) for n, s in services.items()),
                             tuple(dict.fromkeys(env))))
    return found, unreadable


def _read_by_code(top: Path, keys: set[str]) -> set[str]:
    """The environment names the project's own files mention (its CI and its docs aside):
    the ones that switch a code path. Every one, when git cannot say."""
    names = sorted(k for k in keys if _ENV_KEY.match(k))
    if not names:
        return set()
    argv = ["git", "grep", "-I", "-h", "-o", "-w", "-F", *(x for k in names for x in ("-e", k)), "--", ".",
            ":(exclude).github", ":(exclude,glob)**/*.md"]
    try:
        proc = subprocess.run(argv, cwd=str(top), capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set(names)
    if proc.returncode not in (0, 1):  # 1: none found
        return set(names)
    return set(proc.stdout.split()) & set(names)


#: The CI jobs a person said are known not to apply to a repository's checks (`locks ci quiet`),
#: kept per repository key in the store's meta (the person's locks home, never a repository).
QUIET_META = "ci_quiet:"
QUIET_HINT = " (`locks ci quiet <job>` stops naming one known not to apply here)"


def quiet(store: Any, key: str | None) -> set[str]:
    """The jobs `left_out` names no more for this repository."""
    try:
        return set(json.loads(store.meta(QUIET_META + str(key)) or "[]"))
    except ValueError:
        return set()


def set_quiet(store: Any, key: str | None, names: Sequence[str], *, on: bool = True) -> set[str]:
    """Quiet (or speak again of) these jobs for this repository; the jobs quiet after it."""
    now = quiet(store, key)
    now = now | set(names) if on else now - set(names)
    store.set_meta(QUIET_META + str(key), json.dumps(sorted(now)))
    return now


def left_out(where: Path, verify: str, services: Sequence[str] = (),
             quieted: Collection[str] = ()) -> tuple[str | None, list[str]]:
    """The CI jobs `verify` does not cover, as one note, and the workflow files that could
    not be read. A job is covered when the check runs every command of it that checks the
    work (each one a line chains with `&&`, `||` or `;`, `_SETUP` ones aside), through the
    scripts it names, with every service it starts and every environment it sets that the
    project's files read: named in the check, set in
    the environment it runs with, or a service the task starts (`--service`). A job that
    delivers (deploy, release) is no check, and nor is one that only readies itself."""
    found, unreadable = jobs(where)
    checks = {id(j): [c for line in j.commands for c in _parts(line) if not _SETUP.match(c)] for j in found}
    found = [j for j in found if checks[id(j)] and not _DELIVERY.search(j.name)
             and j.name not in quieted and f"{j.file}/{j.name}" not in quieted]
    if not found:
        return None, unreadable
    ran = " \n".join(runs(verify, where))
    flat = " ".join(ran.split())
    started = " ".join(services).lower()
    read = _read_by_code(repo.toplevel(where) or where, {k for j in found for k in j.env})
    twice = {j.name for j in found if sum(k.name == j.name for k in found) > 1}
    missing = []
    for job in found:
        uncovered = [c for c in checks[id(job)] if " ".join(c.split()) not in flat]
        unset = [f"a {s} service" for s in job.services if s.lower() not in flat.lower() and s.lower() not in started]
        unset += [k for k in job.env if k in read and k not in os.environ
                  and not re.search(rf"(?<![\w$]){re.escape(k)}(?!\w)", ran)]
        if not uncovered and not unset:
            continue
        name = f"{job.file}/{job.name}" if job.name in twice else job.name
        what = [f"`{c[:60]}`" for c in uncovered] + unset
        missing.append(f"{name} ({', '.join(what)})")
    if not missing:
        return None, unreadable
    more = len(missing) - JOBS_NAMED
    named = "; ".join(missing[:JOBS_NAMED]) + (f"; and {more} more" if more > 0 else "")
    return f"CI also runs: {named}, which this check does not{QUIET_HINT}", unreadable


# --- a check whose exit says nothing ---------------------------------------------------

#: Commands that end a pipeline and exit 0 whatever came before them: the pipeline's exit is theirs.
_FILTERS = frozenset({"grep", "egrep", "fgrep", "tail", "head", "tee", "sed", "awk", "cat", "sort", "uniq", "wc"})
#: What ends a check so that it always exits 0.
_SWALLOWED = re.compile(r"(\|\|\s*(?:true|:|exit\s+0)|;\s*(?:true|exit\s+0))\s*$")
#: `set -o pipefail`, `set -euo pipefail`, `set -e -o pipefail`.
_PIPEFAIL = re.compile(r"\bset\s+(?:-[a-z]+\s+)*-[a-z]*o\s+pipefail\b")


def _pipeline(part: str) -> list[str]:
    """A command's stages, split at each `|` outside quotes that is no `||`."""
    stages, start, quote, i = [], 0, "", 0
    while i < len(part):
        c = part[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif part.startswith("||", i):
            i += 2
            continue
        elif c == "|":
            stages.append(part[start:i])
            start = i + 1
        i += 1
    return [s.strip() for s in [*stages, part[start:]] if s.strip()]


def weak_line(command: str) -> str | None:
    """What makes one shell line's exit say nothing of what it checks, or None: a pipeline
    whose last command is a filter (`grep -v`, `tail`, `tee`…) with no `pipefail` set,
    or an ending that swallows the exit (`|| true`, `; exit 0`). A pure reading."""
    line = _CONTINUED.sub(" ", command).strip()
    if found := _SWALLOWED.search(line):
        return " ".join(found.group(1).split())
    if _PIPEFAIL.search(line):
        return None
    for part in _parts(line):
        stages = _pipeline(part)
        if len(stages) < 2:
            continue
        words = stages[-1].split()
        name = Path(words[0]).name if words else ""
        if name in _FILTERS:
            return f"{name} -v" if name in ("grep", "egrep", "fgrep") and "-v" in words[1:] else name
    return None


def weak(command: str, where: Path | None = None) -> str | None:
    """The plan's warning for a check whose green proves nothing, or None: the command, or a
    script it runs (`where`'s package.json and deno.json), exits as a filter at the end of
    its pipeline does, or always exits 0. A warning, never a refusal."""
    bodies = runs(command, where) if where is not None else [command]
    for i, body in enumerate(bodies):
        why = weak_line(body)
        if why is None:
            continue
        via = "" if i == 0 else f" (through a script it runs: `{clip(body, 80, one_line=True)}`)"
        if why.startswith(("||", ";")):
            return (f"this task's check `{command}` always exits 0 (`{why}`){via}: a failure never reaches locks, "
                    "so a green check proves nothing; drop it")
        return (f"this task's check `{command}` exits as its last command (`{why}`) does{via}: a failure before "
                "it never reaches locks, so a green check proves nothing; add `set -o pipefail` or drop the pipe")
    return None


#: What runs a tool from a project's dependencies, before the tool's own name.
_LAUNCHERS = (("npm", "exec"), ("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "exec"), ("npx",), ("bunx",), ("yarn",),
              ("pnpm",))
#: The scripts a person most likely means by "the repository's check", first.
_CHECK_SCRIPTS = ("test", "test:ci", "ci", "check", "verify", "lint", "build")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _tool(part: str) -> list[str]:
    """One command's words from its tool on: environment assignments and a launcher left out."""
    words = [w for w in part.split() if not _ENV_ASSIGN.match(w)]
    for launcher in _LAUNCHERS:
        if tuple(words[:len(launcher)]) == launcher and len(words) > len(launcher):
            words = words[len(launcher):]
            break
    return [Path(words[0]).name, *words[1:]] if words else []


def bypassed(command: str, where: Path | None) -> str | None:
    """The plan's warning for a check that runs a tool itself while a script of the project
    runs the same tool with more (`ng test` against `npm test`'s `ng test --code-coverage`):
    what the script adds, a coverage threshold, a config, a project, is not checked. None
    when the check goes through the project's scripts, or no script runs its tool with more.
    A warning, never a refusal."""
    if where is None or len(runs(command, where)) > 1:
        return None
    mine = [_tool(part) for part in _parts(_CONTINUED.sub(" ", command))]
    npm, deno = _package_scripts(where), _deno_tasks(where)
    named = [(name, body, "npm") for name, body in npm.items()] + [(name, body, "deno") for name, body in deno.items()]
    order = {name: i for i, name in enumerate(_CHECK_SCRIPTS)}
    named.sort(key=lambda found: (order.get(found[0], len(order)), found[0]))
    for name, body, runner in named:
        for theirs in (_tool(part) for part in _parts(_CONTINUED.sub(" ", body))):
            for words in mine:
                if not words or not theirs or words[0] != theirs[0] or (len(words) > 1 and len(theirs) > 1
                                                                       and words[1] != theirs[1]):
                    continue
                extra = [w for w in theirs[1:] if w not in words[1:]]
                if not extra:
                    continue
                call = ("npm test" if name == "test" else f"npm run {name}") if runner == "npm" else f"deno task {name}"
                return (f"this task's check `{clip(command, 80, one_line=True)}` runs `{words[0]}` itself, while "
                        f"`{call}` runs `{clip(' '.join(theirs), 100, one_line=True)}`: what it adds "
                        f"({' '.join(extra[:4])}{' …' if len(extra) > 4 else ''}) is not checked here, a coverage "
                        f"threshold or a config among them; `--verify '{call}'` checks what the repository checks")
    return None


#: How a runner the check uses runs only the tests near some files: `{files}` is where they go.
_RELATED = (
    ("vitest", "npx vitest related --run {files}"),
    ("jest", "npx jest --findRelatedTests {files}"),
    ("pytest", "python -m pytest -x {files}"),
)


def related(command: str, where: Path | None) -> tuple[str, str] | None:
    """(runner, the command that runs only the tests near what changed) for the test runner
    the check runs, directly or through the scripts it names; None when it runs none locks
    knows. A worker runs it as it goes; the check itself runs in full at the end."""
    bodies = runs(command, where) if where is not None else [command]
    for body in bodies:
        for part in _parts(_CONTINUED.sub(" ", body)):
            words = _tool(part)
            if not words:
                continue
            tool = words[0]
            if tool in ("python", "python3") and words[1:3] == ["-m", "pytest"]:
                tool = "pytest"
            for runner, line in _RELATED:
                if tool == runner:
                    return runner, line
    return None
